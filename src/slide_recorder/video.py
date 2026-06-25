from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import imageio_ffmpeg
import numpy as np
import pymupdf

from .storage import Slide, write_wav_mono


ProgressCallback = Callable[[str, int, int], None]

DEFAULT_VIDEO_SIZE = (1920, 1080)
DEFAULT_FPS = 30
SILENT_SLIDE_SECONDS = 1.0
DEFAULT_VIDEO_CODEC_KEY = "h264"


class VideoExportError(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoExportResult:
    output_path: Path
    slide_count: int
    duration_seconds: float
    silent_slide_count: int
    codec_label: str
    encoder_label: str


@dataclass(frozen=True)
class VideoEncoder:
    label: str
    encoder: str
    hardware: bool
    ffmpeg_args: tuple[str, ...]


@dataclass(frozen=True)
class VideoCodec:
    key: str
    label: str
    software: VideoEncoder
    hardware: tuple[VideoEncoder, ...]


VIDEO_CODECS = (
    VideoCodec(
        key="h264",
        label="H.264 / AVC (best compatibility)",
        software=VideoEncoder(
            label="H.264 software (libx264)",
            encoder="libx264",
            hardware=False,
            ffmpeg_args=("-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-crf", "20"),
        ),
        hardware=(
            VideoEncoder("H.264 hardware (VideoToolbox)", "h264_videotoolbox", True, ("-c:v", "h264_videotoolbox", "-b:v", "6000k")),
            VideoEncoder("H.264 hardware (NVENC)", "h264_nvenc", True, ("-c:v", "h264_nvenc", "-b:v", "6000k")),
            VideoEncoder("H.264 hardware (AMF)", "h264_amf", True, ("-c:v", "h264_amf", "-b:v", "6000k")),
            VideoEncoder("H.264 hardware (QSV)", "h264_qsv", True, ("-c:v", "h264_qsv", "-b:v", "6000k")),
        ),
    ),
    VideoCodec(
        key="h265",
        label="H.265 / HEVC (smaller files)",
        software=VideoEncoder(
            label="H.265 software (libx265)",
            encoder="libx265",
            hardware=False,
            ffmpeg_args=("-c:v", "libx265", "-preset", "medium", "-crf", "26", "-tag:v", "hvc1"),
        ),
        hardware=(
            VideoEncoder("H.265 hardware (VideoToolbox)", "hevc_videotoolbox", True, ("-c:v", "hevc_videotoolbox", "-b:v", "4500k", "-tag:v", "hvc1")),
            VideoEncoder("H.265 hardware (NVENC)", "hevc_nvenc", True, ("-c:v", "hevc_nvenc", "-b:v", "4500k", "-tag:v", "hvc1")),
            VideoEncoder("H.265 hardware (AMF)", "hevc_amf", True, ("-c:v", "hevc_amf", "-b:v", "4500k", "-tag:v", "hvc1")),
            VideoEncoder("H.265 hardware (QSV)", "hevc_qsv", True, ("-c:v", "hevc_qsv", "-b:v", "4500k", "-tag:v", "hvc1")),
        ),
    ),
    VideoCodec(
        key="av1",
        label="AV1 (smallest, slowest)",
        software=VideoEncoder(
            label="AV1 software (libaom-av1)",
            encoder="libaom-av1",
            hardware=False,
            ffmpeg_args=("-c:v", "libaom-av1", "-crf", "34", "-b:v", "0", "-cpu-used", "6", "-row-mt", "1"),
        ),
        hardware=(
            VideoEncoder("AV1 hardware (NVENC)", "av1_nvenc", True, ("-c:v", "av1_nvenc", "-b:v", "3000k")),
            VideoEncoder("AV1 hardware (AMF)", "av1_amf", True, ("-c:v", "av1_amf", "-b:v", "3000k")),
            VideoEncoder("AV1 hardware (QSV)", "av1_qsv", True, ("-c:v", "av1_qsv", "-b:v", "3000k")),
        ),
    ),
)


def pdf_page_count(pdf_path: Path) -> int:
    try:
        document = pymupdf.open(pdf_path)
    except Exception as exc:
        raise VideoExportError(f"Could not open PDF: {exc}") from exc
    try:
        return int(document.page_count)
    finally:
        document.close()


def available_video_codecs() -> list[VideoCodec]:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    encoders = _ffmpeg_encoders(ffmpeg)
    available = [
        codec
        for codec in VIDEO_CODECS
        if any(encoder.encoder in encoders for encoder in _encoder_options(codec))
    ]
    return available or [resolve_video_codec(DEFAULT_VIDEO_CODEC_KEY)]


def resolve_video_codec(codec_key: str) -> VideoCodec:
    return next((codec for codec in VIDEO_CODECS if codec.key == codec_key), VIDEO_CODECS[0])


def export_pdf_slide_video(
    pdf_path: Path,
    destination: Path,
    slides: Sequence[Slide],
    sample_rate: int,
    *,
    size: tuple[int, int] = DEFAULT_VIDEO_SIZE,
    fps: int = DEFAULT_FPS,
    codec_key: str = DEFAULT_VIDEO_CODEC_KEY,
    progress: ProgressCallback | None = None,
) -> VideoExportResult:
    if not slides:
        raise VideoExportError("There are no slides to export.")
    if sample_rate <= 0:
        raise VideoExportError("Invalid session sample rate.")

    pdf_path = Path(pdf_path)
    destination = Path(destination)
    width, height = size
    if width <= 0 or height <= 0:
        raise VideoExportError("Invalid video size.")
    if fps <= 0:
        raise VideoExportError("Invalid video frame rate.")

    total_steps = len(slides) * 2 + 1
    _emit_progress(progress, "Opening PDF...", 0, total_steps)
    try:
        document = pymupdf.open(pdf_path)
    except Exception as exc:
        raise VideoExportError(f"Could not open PDF: {exc}") from exc

    try:
        page_count = int(document.page_count)
        if page_count != len(slides):
            raise VideoExportError(
                f"PDF page count must match slide count. PDF has {page_count} pages; "
                f"the session has {len(slides)} slides."
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        codec = resolve_video_codec(codec_key)
        encode_options = _available_encoder_options(codec, _ffmpeg_encoders(ffmpeg))
        if not encode_options:
            raise VideoExportError(f"The bundled ffmpeg does not support {codec.label}.")
        active_encoder = encode_options[0]
        total_duration = 0.0
        silent_slide_count = 0

        with TemporaryDirectory(prefix="slide_recorder_video_") as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            segment_paths: list[Path] = []

            for index, slide in enumerate(slides):
                slide_number = index + 1
                image_path = temp_dir / f"slide_{slide_number:04d}.png"
                audio_path = temp_dir / f"slide_{slide_number:04d}.wav"
                segment_path = temp_dir / f"slide_{slide_number:04d}.mp4"

                _emit_progress(progress, f"Rendering PDF page {slide_number}...", index * 2, total_steps)
                _render_page(document, index, image_path, width, height)

                samples = slide.samples.astype(np.float32, copy=False)
                if samples.size == 0:
                    silent_slide_count += 1
                    samples = np.zeros(round(sample_rate * SILENT_SLIDE_SECONDS), dtype=np.float32)
                duration = max(samples.size / float(sample_rate), 1.0 / float(fps))
                total_duration += duration
                write_wav_mono(audio_path, samples, sample_rate)

                _emit_progress(progress, f"Encoding slide {slide_number}...", index * 2 + 1, total_steps)
                used_encoder = _encode_segment(
                    ffmpeg=ffmpeg,
                    image_path=image_path,
                    audio_path=audio_path,
                    segment_path=segment_path,
                    duration=duration,
                    width=width,
                    height=height,
                    fps=fps,
                    encoders=[active_encoder, *[encoder for encoder in encode_options if encoder != active_encoder]],
                )
                active_encoder = used_encoder
                segment_paths.append(segment_path)

            _emit_progress(progress, "Combining slide video...", total_steps - 1, total_steps)
            _concat_segments(ffmpeg, segment_paths, destination)
            _emit_progress(progress, "Video export complete.", total_steps, total_steps)

        return VideoExportResult(
            output_path=destination,
            slide_count=len(slides),
            duration_seconds=total_duration,
            silent_slide_count=silent_slide_count,
            codec_label=codec.label,
            encoder_label=active_encoder.label,
        )
    finally:
        document.close()


def _render_page(document, page_index: int, image_path: Path, width: int, height: int) -> None:
    page = document.load_page(page_index)
    rect = page.rect
    scale = min(width / max(rect.width, 1.0), height / max(rect.height, 1.0))
    matrix = pymupdf.Matrix(scale, scale)
    pixmap = page.get_pixmap(matrix=matrix, alpha=False, colorspace=pymupdf.csRGB)
    pixmap.save(image_path)


def _encode_segment(
    *,
    ffmpeg: str,
    image_path: Path,
    audio_path: Path,
    segment_path: Path,
    duration: float,
    width: int,
    height: int,
    fps: int,
    encoders: Sequence[VideoEncoder],
) -> VideoEncoder:
    video_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=white,"
        "format=yuv420p"
    )
    last_error: VideoExportError | None = None
    for encoder in encoders:
        try:
            _run_ffmpeg(
                [
                    ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-loop",
                    "1",
                    "-framerate",
                    str(fps),
                    "-i",
                    str(image_path),
                    "-i",
                    str(audio_path),
                    "-t",
                    f"{duration:.6f}",
                    "-vf",
                    video_filter,
                    "-r",
                    str(fps),
                    *encoder.ffmpeg_args,
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-pix_fmt",
                    "yuv420p",
                    "-shortest",
                    str(segment_path),
                ],
                f"Could not encode slide video segment {segment_path.name} with {encoder.label}.",
            )
            return encoder
        except VideoExportError as exc:
            segment_path.unlink(missing_ok=True)
            last_error = exc
            if not encoder.hardware:
                break
    if last_error is not None:
        raise last_error
    raise VideoExportError(f"No encoder is available for slide video segment {segment_path.name}.")


def _concat_segments(ffmpeg: str, segment_paths: Sequence[Path], destination: Path) -> None:
    if not segment_paths:
        raise VideoExportError("No video segments were created.")

    list_path = destination.parent / f".{destination.stem}_concat.txt"
    try:
        list_path.write_text(
            "".join(f"file '{_escape_concat_path(path)}'\n" for path in segment_paths),
            encoding="utf-8",
        )
        _run_ffmpeg(
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(destination),
            ],
            "Could not combine slide video segments.",
        )
    finally:
        list_path.unlink(missing_ok=True)


def _run_ffmpeg(command: list[str], failure_message: str) -> None:
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode == 0:
        return

    output = (result.stderr or result.stdout or "").strip()
    if len(output) > 2000:
        output = output[-2000:]
    details = f"\n\nffmpeg output:\n{output}" if output else ""
    raise VideoExportError(f"{failure_message}{details}")


def _ffmpeg_encoders(ffmpeg: str) -> set[str]:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return set()
    encoders: set[str] = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            encoders.add(parts[1])
    return encoders


def _encoder_options(codec: VideoCodec) -> tuple[VideoEncoder, ...]:
    return (*codec.hardware, codec.software)


def _available_encoder_options(codec: VideoCodec, encoders: set[str]) -> list[VideoEncoder]:
    return [encoder for encoder in _encoder_options(codec) if encoder.encoder in encoders]


def _escape_concat_path(path: Path) -> str:
    return str(path.resolve()).replace("'", r"'\''")


def _emit_progress(progress: ProgressCallback | None, message: str, value: int, maximum: int) -> None:
    if progress is not None:
        progress(message, value, maximum)
