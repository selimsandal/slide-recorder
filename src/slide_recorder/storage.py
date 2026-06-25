from __future__ import annotations

import json
import shutil
import wave
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile

import numpy as np


DEFAULT_SLIDE_COUNT = 12


@dataclass
class AudioClip:
    id: int
    start_sample: int
    file_name: str
    layer: int = 0
    samples: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.float32), repr=False)

    @property
    def end_sample(self) -> int:
        return self.start_sample + int(self.samples.size)


@dataclass
class Slide:
    id: int
    title: str
    file_name: str
    clips: list[AudioClip] = field(default_factory=list, repr=False)

    @property
    def has_audio(self) -> bool:
        return any(clip.samples.size > 0 for clip in self.clips)

    @property
    def samples(self) -> np.ndarray:
        return mix_clips(self.clips)

    @samples.setter
    def samples(self, value: np.ndarray) -> None:
        samples = np.asarray(value, dtype=np.float32)
        if samples.size == 0:
            self.clips = []
            return
        self.clips = [
            AudioClip(
                id=0,
                start_sample=0,
                file_name=f"{Path(self.file_name).stem}_clip_000.wav",
                layer=0,
                samples=samples.copy(),
            )
        ]


class SessionStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.recordings_dir = directory / "recordings"
        self.session_path = directory / "session.json"
        self.sample_rate = 48_000
        self.next_slide_id = 1
        self.next_clip_id = 1
        self.next_clip_layer = 1
        self.ask_confirm_delete = True
        self.slides: list[Slide] = []

    @classmethod
    def load(cls, directory: Path) -> "SessionStore":
        store = cls(directory)
        store.directory.mkdir(parents=True, exist_ok=True)
        store.recordings_dir.mkdir(parents=True, exist_ok=True)

        if store.session_path.exists():
            store._load_existing()
        else:
            for _ in range(DEFAULT_SLIDE_COUNT):
                store.add_slide(save=False)
            store.save()

        if not store.slides:
            store.add_slide(save=False)
            store.save()
        return store

    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "sample_rate": self.sample_rate,
            "next_slide_id": self.next_slide_id,
            "next_clip_id": self.next_clip_id,
            "next_clip_layer": self.next_clip_layer,
            "ask_confirm_delete": self.ask_confirm_delete,
            "slides": [
                {
                    "id": slide.id,
                    "title": slide.title,
                    "file": slide.file_name,
                    "clips": [
                        {
                            "id": clip.id,
                            "start_sample": clip.start_sample,
                            "layer": clip.layer,
                            "file": clip.file_name,
                        }
                        for clip in slide.clips
                        if clip.samples.size > 0
                    ],
                }
                for slide in self.slides
            ],
        }
        with NamedTemporaryFile("w", encoding="utf-8", dir=self.directory, delete=False) as tmp:
            json.dump(payload, tmp, indent=2)
            tmp.write("\n")
            temp_name = tmp.name
        Path(temp_name).replace(self.session_path)

    def save_slide_audio(self, slide: Slide) -> None:
        mix_path = self.audio_path(slide)
        for clip in slide.clips:
            clip_path = self.clip_path(clip)
            if clip.samples.size == 0:
                clip_path.unlink(missing_ok=True)
            else:
                write_wav_mono(clip_path, clip.samples, self.sample_rate)

        mixed = slide.samples
        if mixed.size == 0:
            mix_path.unlink(missing_ok=True)
        else:
            write_wav_mono(mix_path, mixed, self.sample_rate)
        self.save()

    def audio_path(self, slide: Slide) -> Path:
        return self.recordings_dir / slide.file_name

    def clip_path(self, clip: AudioClip) -> Path:
        return self.recordings_dir / clip.file_name

    def add_slide(self, save: bool = True) -> Slide:
        slide_id = self.next_slide_id
        self.next_slide_id += 1
        slide = Slide(
            id=slide_id,
            title=f"Slide {len(self.slides) + 1}",
            file_name=f"slide_{slide_id:03d}.wav",
        )
        self.slides.append(slide)
        if save:
            self.save()
        return slide

    def remove_slide(self, slide: Slide) -> None:
        self.audio_path(slide).unlink(missing_ok=True)
        for clip in slide.clips:
            self.clip_path(clip).unlink(missing_ok=True)
        self.slides = [candidate for candidate in self.slides if candidate.id != slide.id]
        if not self.slides:
            self.add_slide(save=False)
        self.save()

    def add_clip(self, slide: Slide, start_sample: int, samples: np.ndarray, save: bool = True) -> AudioClip:
        clip = self._make_clip(slide, start_sample, samples)
        if clip.samples.size == 0:
            return clip
        slide.clips.append(clip)
        slide.clips.sort(key=lambda candidate: (candidate.start_sample, candidate.layer, candidate.id))
        if save:
            self.save_slide_audio(slide)
        return clip

    def move_clip(self, slide: Slide, clip_id: int, start_sample: int, save: bool = True) -> None:
        clip = self.clip_by_id(slide, clip_id)
        if clip is None:
            return
        clip.start_sample = max(0, int(start_sample))
        slide.clips.sort(key=lambda candidate: (candidate.start_sample, candidate.layer, candidate.id))
        if save:
            self.save_slide_audio(slide)

    def bring_clip_to_front(self, slide: Slide, clip_id: int, save: bool = True) -> bool:
        changed = self._move_clip_priority(slide, clip_id, to_front=True)
        if changed and save:
            self.save_slide_audio(slide)
        return changed

    def send_clip_to_back(self, slide: Slide, clip_id: int, save: bool = True) -> bool:
        changed = self._move_clip_priority(slide, clip_id, to_front=False)
        if changed and save:
            self.save_slide_audio(slide)
        return changed

    def remove_clip(self, slide: Slide, clip_id: int, save: bool = True) -> None:
        removed = [clip for clip in slide.clips if clip.id == clip_id]
        slide.clips = [clip for clip in slide.clips if clip.id != clip_id]
        for clip in removed:
            self.clip_path(clip).unlink(missing_ok=True)
        if save:
            self.save_slide_audio(slide)

    def replace_slide_clips(self, slide: Slide, clips: list[AudioClip], save: bool = True) -> None:
        self._install_clips(slide, clips)
        if save:
            self.save_slide_audio(slide)

    def clip_by_id(self, slide: Slide, clip_id: int) -> AudioClip | None:
        return next((clip for clip in slide.clips if clip.id == clip_id), None)

    def export_slide(self, slide: Slide, destination: Path) -> None:
        self.save_slide_audio(slide)
        source = self.audio_path(slide)
        if not source.exists():
            raise ValueError("The selected slide does not have a recording.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    def export_all_zip(self, destination: Path) -> int:
        count = 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for index, slide in enumerate(self.slides, start=1):
                self.save_slide_audio(slide)
                source = self.audio_path(slide)
                if not source.exists():
                    continue
                archive.write(source, arcname=f"{index:03d}_{safe_file_stem(slide.title)}.wav")
                count += 1
        return count

    def has_any_audio(self) -> bool:
        return any(slide.has_audio for slide in self.slides)

    def _load_existing(self) -> None:
        data = json.loads(self.session_path.read_text(encoding="utf-8"))
        self.sample_rate = int(data.get("sample_rate", 48_000))
        self.next_slide_id = int(data.get("next_slide_id", 1))
        self.next_clip_id = int(data.get("next_clip_id", 1))
        self.next_clip_layer = int(data.get("next_clip_layer", self.next_clip_id))
        self.ask_confirm_delete = bool(data.get("ask_confirm_delete", True))
        self.slides = []
        migrated_legacy_audio = False

        for slide_data in data.get("slides", []):
            slide = Slide(
                id=int(slide_data["id"]),
                title=str(slide_data.get("title", f"Slide {len(self.slides) + 1}")),
                file_name=str(slide_data.get("file", f"slide_{int(slide_data['id']):03d}.wav")),
            )
            for clip_data in slide_data.get("clips", []):
                clip = AudioClip(
                    id=int(clip_data["id"]),
                    start_sample=max(0, int(clip_data.get("start_sample", 0))),
                    file_name=str(clip_data.get("file", "")),
                    layer=max(0, int(clip_data.get("layer", clip_data.get("id", 0)))),
                )
                if not clip.file_name:
                    clip.file_name = self._clip_file_name(slide, clip.id)
                path = self.clip_path(clip)
                if path.exists():
                    samples, sample_rate = read_wav_mono(path)
                    if sample_rate != self.sample_rate:
                        samples = resample_linear(samples, sample_rate, self.sample_rate)
                    clip.samples = samples
                    if clip.samples.size:
                        slide.clips.append(clip)
                self.next_clip_id = max(self.next_clip_id, clip.id + 1)
                self.next_clip_layer = max(self.next_clip_layer, clip.layer + 1)

            if not slide.clips:
                path = self.audio_path(slide)
                if path.exists():
                    samples, sample_rate = read_wav_mono(path)
                    if sample_rate != self.sample_rate:
                        samples = resample_linear(samples, sample_rate, self.sample_rate)
                    if samples.size:
                        slide.clips.append(self._make_clip(slide, 0, samples))
                        migrated_legacy_audio = True
            self.slides.append(slide)

        self.next_slide_id = max(self.next_slide_id, *(slide.id + 1 for slide in self.slides), 1)
        self.next_clip_id = max(self.next_clip_id, *(clip.id + 1 for slide in self.slides for clip in slide.clips), 1)
        self.next_clip_layer = max(
            self.next_clip_layer,
            *(clip.layer + 1 for slide in self.slides for clip in slide.clips),
            1,
        )
        if migrated_legacy_audio:
            for slide in self.slides:
                self.save_slide_audio(slide)

    def _make_clip(
        self,
        slide: Slide,
        start_sample: int,
        samples: np.ndarray,
        layer: int | None = None,
    ) -> AudioClip:
        clip_id = self.next_clip_id
        self.next_clip_id += 1
        clip_layer = layer if layer is not None else self._next_layer()
        self.next_clip_layer = max(self.next_clip_layer, clip_layer + 1)
        return AudioClip(
            id=clip_id,
            start_sample=max(0, int(start_sample)),
            file_name=self._clip_file_name(slide, clip_id),
            layer=clip_layer,
            samples=np.asarray(samples, dtype=np.float32).copy(),
        )

    @staticmethod
    def _clip_file_name(slide: Slide, clip_id: int) -> str:
        return f"slide_{slide.id:03d}_clip_{clip_id:04d}.wav"

    def _install_clips(self, slide: Slide, clips: list[AudioClip]) -> None:
        for clip in slide.clips:
            self.clip_path(clip).unlink(missing_ok=True)

        installed: list[AudioClip] = []
        used_ids: set[int] = set()
        for source in sorted(clips, key=lambda clip: (clip.start_sample, clip.layer, clip.id)):
            if source.samples.size == 0:
                continue
            if source.id > 0 and source.id not in used_ids:
                clip_id = source.id
                used_ids.add(clip_id)
                self.next_clip_id = max(self.next_clip_id, clip_id + 1)
                self.next_clip_layer = max(self.next_clip_layer, source.layer + 1)
                installed.append(
                    AudioClip(
                        id=clip_id,
                        start_sample=max(0, int(source.start_sample)),
                        file_name=source.file_name or self._clip_file_name(slide, clip_id),
                        layer=source.layer,
                        samples=np.asarray(source.samples, dtype=np.float32).copy(),
                    )
                )
            else:
                installed.append(self._make_clip(slide, source.start_sample, source.samples, layer=source.layer))

        slide.clips = sorted(installed, key=lambda clip: (clip.start_sample, clip.layer, clip.id))

    def _next_layer(self) -> int:
        layer = self.next_clip_layer
        self.next_clip_layer += 1
        return layer

    def _move_clip_priority(self, slide: Slide, clip_id: int, *, to_front: bool) -> bool:
        ordered = sorted(slide.clips, key=lambda clip: (clip.layer, clip.id))
        target = next((clip for clip in ordered if clip.id == clip_id), None)
        if target is None:
            return False

        old_order = [clip.id for clip in ordered]
        without_target = [clip for clip in ordered if clip.id != clip_id]
        reordered = without_target + [target] if to_front else [target] + without_target
        if [clip.id for clip in reordered] == old_order:
            return False

        for layer, clip in enumerate(reordered):
            clip.layer = layer
        self.next_clip_layer = max(self.next_clip_layer, len(reordered))
        slide.clips.sort(key=lambda candidate: (candidate.start_sample, candidate.layer, candidate.id))
        return True


def clone_clips(clips: list[AudioClip]) -> list[AudioClip]:
    return [
        AudioClip(
            id=clip.id,
            start_sample=clip.start_sample,
            file_name=clip.file_name,
            layer=clip.layer,
            samples=clip.samples.copy(),
        )
        for clip in clips
    ]


def mix_clips(clips: list[AudioClip], priority_clip_id: int | None = None) -> np.ndarray:
    duration = max((clip.end_sample for clip in clips if clip.samples.size > 0), default=0)
    if duration <= 0:
        return np.empty(0, dtype=np.float32)

    mixed = np.zeros(duration, dtype=np.float32)
    ordered = sorted(
        [clip for clip in clips if clip.samples.size > 0],
        key=lambda clip: (clip.id == priority_clip_id, clip.layer, clip.id),
    )
    for clip in ordered:
        if clip.samples.size == 0:
            continue
        start = max(0, int(clip.start_sample))
        end = min(duration, start + int(clip.samples.size))
        if end <= start:
            continue
        mixed[start:end] = clip.samples[: end - start]
    return np.clip(mixed, -1.0, 1.0).astype(np.float32, copy=False)


def read_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        frames = wav_file.readframes(wav_file.getnframes())

    if sample_width == 1:
        audio = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sample_width == 2:
        audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 3:
        raw = np.frombuffer(frames, dtype=np.uint8).reshape(-1, 3)
        values = (
            raw[:, 0].astype(np.int32)
            | (raw[:, 1].astype(np.int32) << 8)
            | (raw[:, 2].astype(np.int32) << 16)
        )
        values = np.where(values & 0x800000, values - 0x1000000, values)
        audio = values.astype(np.float32) / 8_388_608.0
    elif sample_width == 4:
        audio = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2_147_483_648.0
    else:
        raise ValueError(f"Unsupported WAV sample width: {sample_width}")

    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)

    return np.clip(audio, -1.0, 1.0).astype(np.float32), sample_rate


def write_wav_mono(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(samples, -1.0, 1.0)
    pcm16 = (pcm * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm16.tobytes())


def resample_linear(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate or samples.size == 0:
        return samples.astype(np.float32, copy=False)

    duration = samples.size / float(source_rate)
    target_size = max(1, int(round(duration * target_rate)))
    source_positions = np.linspace(0.0, samples.size - 1, num=samples.size, dtype=np.float64)
    target_positions = np.linspace(0.0, samples.size - 1, num=target_size, dtype=np.float64)
    return np.interp(target_positions, source_positions, samples).astype(np.float32)


def safe_file_stem(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in ("-", "_") else "_" for character in value)
    cleaned = cleaned.strip("_")
    return cleaned or "slide"


def format_duration(samples: np.ndarray, sample_rate: int) -> str:
    total_seconds = samples.size / float(sample_rate)
    minutes = int(total_seconds // 60)
    seconds = total_seconds - (minutes * 60)
    return f"{minutes:02d}:{seconds:05.2f}"
