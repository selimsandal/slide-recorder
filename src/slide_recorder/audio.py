from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QObject, QTimer
from PySide6.QtMultimedia import QAudioDevice, QAudioFormat, QAudioSink, QAudioSource, QMediaDevices


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    channels: int
    default_sample_rate: int


class AudioEngine(QObject):
    """Keeps the microphone stream open and records only while armed."""

    def __init__(self, sample_rate: int = 48_000, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.sample_rate = sample_rate
        self.input_device: int | None = None

        self._input_source: QAudioSource | None = None
        self._input_io = None
        self._input_format = self._make_format(sample_rate, 1)
        self._input_remainder = b""

        self._chunks: deque[np.ndarray] = deque()
        self._recording = False
        self._recorded_frames = 0
        self._level = 0.0
        self._last_status = ""

        self._playback_sink: QAudioSink | None = None
        self._playback_io = None
        self._playback_bytes = b""
        self._playback_offset = 0
        self._playback_started_at = 0.0
        self._playback_duration = 0.0
        self._playback_timer = QTimer(self)
        self._playback_timer.setInterval(20)
        self._playback_timer.timeout.connect(self._pump_playback)

    @staticmethod
    def input_devices() -> list[AudioDevice]:
        devices: list[AudioDevice] = []
        for index, device in enumerate(QMediaDevices.audioInputs()):
            preferred = device.preferredFormat()
            devices.append(
                AudioDevice(
                    index=index,
                    name=device.description(),
                    channels=max(1, preferred.channelCount()),
                    default_sample_rate=preferred.sampleRate() or 48_000,
                )
            )
        return devices

    def start(self, input_device: int | None = None, sample_rate: int | None = None) -> None:
        if self._input_source is not None:
            return

        self.input_device = input_device
        requested_rate = sample_rate or self.sample_rate
        self._open_stream(requested_rate)
        self.sample_rate = self._input_format.sampleRate()

    def restart(self, input_device: int | None = None, sample_rate: int | None = None) -> None:
        was_recording = self.is_recording
        if was_recording:
            self.stop_recording()
        self.stop()
        self.start(input_device=input_device, sample_rate=sample_rate)

    def stop(self) -> None:
        source = self._input_source
        self._input_source = None
        self._input_io = None
        self._input_remainder = b""
        if source is not None:
            source.stop()
            source.deleteLater()

    def start_recording(self) -> None:
        if self._input_source is None:
            self.start(input_device=self.input_device, sample_rate=self.sample_rate)
        self._chunks.clear()
        self._recorded_frames = 0
        self._recording = True

    def stop_recording(self) -> np.ndarray:
        self._recording = False
        chunks = list(self._chunks)
        self._chunks.clear()
        self._recorded_frames = 0

        if not chunks:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(chunks).astype(np.float32, copy=False)

    def recording_snapshot(self) -> np.ndarray:
        if not self._chunks:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(list(self._chunks)).astype(np.float32, copy=False)

    def discard_recording(self) -> None:
        self._recording = False
        self._chunks.clear()
        self._recorded_frames = 0

    def play(self, samples: np.ndarray, sample_rate: int) -> None:
        self.stop_playback()
        if samples.size == 0:
            return

        output_device = QMediaDevices.defaultAudioOutput()
        if output_device.isNull():
            self._last_status = "No audio output device found."
            return

        output_format = self._format_for_device(output_device, sample_rate, output=True)
        playable = np.asarray(samples, dtype=np.float32)
        if output_format.sampleRate() != sample_rate:
            playable = _resample_linear(playable, sample_rate, output_format.sampleRate())

        self._playback_bytes = self._float_to_bytes(playable, output_format)
        self._playback_offset = 0
        self._playback_sink = QAudioSink(output_device, output_format, self)
        self._playback_sink.setBufferSize(min(max(len(self._playback_bytes), 4096), 1_048_576))
        self._playback_io = self._playback_sink.start()
        self._playback_started_at = time.monotonic()
        self._playback_duration = float(playable.size) / float(output_format.sampleRate())
        self._playback_timer.start()
        self._pump_playback()

    def stop_playback(self) -> None:
        self._playback_timer.stop()
        sink = self._playback_sink
        self._playback_sink = None
        self._playback_io = None
        self._playback_bytes = b""
        self._playback_offset = 0
        self._playback_started_at = 0.0
        self._playback_duration = 0.0
        if sink is not None:
            sink.stop()
            sink.deleteLater()

    @property
    def is_playing(self) -> bool:
        if self._playback_sink is None or self._playback_started_at == 0.0:
            return False
        if self._playback_offset < len(self._playback_bytes):
            return True
        return (time.monotonic() - self._playback_started_at) < self._playback_duration + 0.1

    @property
    def playback_elapsed_seconds(self) -> float:
        if self._playback_started_at == 0.0:
            return 0.0
        return min(time.monotonic() - self._playback_started_at, self._playback_duration)

    @property
    def playback_duration_seconds(self) -> float:
        return self._playback_duration

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def recorded_seconds(self) -> float:
        return float(self._recorded_frames) / float(self.sample_rate)

    @property
    def level(self) -> float:
        return self._level

    @property
    def last_status(self) -> str:
        return self._last_status

    def _open_stream(self, sample_rate: int) -> None:
        device = self._input_device_by_index(self.input_device)
        if device.isNull():
            raise RuntimeError("No microphone input device found.")

        self._input_format = self._format_for_device(device, sample_rate, output=False)
        self.sample_rate = self._input_format.sampleRate()
        source = QAudioSource(device, self._input_format, self)
        source.setBufferSize(max(self._input_format.bytesPerFrame() * self.sample_rate // 10, 4096))
        io_device = source.start()
        if io_device is None:
            source.deleteLater()
            raise RuntimeError("Could not start microphone input.")
        io_device.readyRead.connect(self._read_input)
        self._input_source = source
        self._input_io = io_device

    def _read_input(self) -> None:
        if self._input_io is None:
            return

        raw = self._input_remainder + bytes(self._input_io.readAll())
        frame_size = max(1, self._input_format.bytesPerFrame())
        complete_size = len(raw) - (len(raw) % frame_size)
        if complete_size <= 0:
            self._input_remainder = raw
            return

        frame_bytes = raw[:complete_size]
        self._input_remainder = raw[complete_size:]
        samples = self._bytes_to_float(frame_bytes, self._input_format)
        if samples.size:
            level = float(np.sqrt(np.mean(samples * samples)))
        else:
            level = 0.0

        self._level = min(1.0, level)
        if self._recording:
            self._chunks.append(samples)
            self._recorded_frames += int(samples.size)

    def _pump_playback(self) -> None:
        if self._playback_sink is None or self._playback_io is None:
            self.stop_playback()
            return

        available = self._playback_sink.bytesFree()
        remaining = len(self._playback_bytes) - self._playback_offset
        if available > 0 and remaining > 0:
            chunk_size = min(available, remaining)
            written = self._playback_io.write(
                self._playback_bytes[self._playback_offset : self._playback_offset + chunk_size]
            )
            if written > 0:
                self._playback_offset += int(written)

        finished_writing = self._playback_offset >= len(self._playback_bytes)
        elapsed = time.monotonic() - self._playback_started_at
        if finished_writing and elapsed >= self._playback_duration + 0.1:
            self.stop_playback()

    def _input_device_by_index(self, index: int | None) -> QAudioDevice:
        inputs = QMediaDevices.audioInputs()
        if index is not None and 0 <= index < len(inputs):
            return inputs[index]
        return QMediaDevices.defaultAudioInput()

    def _format_for_device(self, device: QAudioDevice, sample_rate: int, output: bool) -> QAudioFormat:
        preferred = device.preferredFormat()
        requested_channels = self._output_channel_count(device, preferred) if output else 1
        requested = self._make_format(sample_rate, requested_channels)
        if device.isFormatSupported(requested):
            return requested

        if output and requested_channels > 1:
            preferred_stereo = self._make_format(preferred.sampleRate() or sample_rate, requested_channels)
            if device.isFormatSupported(preferred_stereo):
                self._last_status = "Using stereo output for mono recording playback."
                return preferred_stereo

        if preferred.sampleFormat() == QAudioFormat.SampleFormat.Unknown:
            preferred.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if preferred.channelCount() < 1:
            preferred.setChannelCount(1)
        if preferred.sampleRate() < 1:
            preferred.setSampleRate(sample_rate)

        self._last_status = "Using device preferred audio format."
        if output:
            return preferred
        return preferred

    @staticmethod
    def _output_channel_count(device: QAudioDevice, preferred: QAudioFormat) -> int:
        try:
            max_channels = device.maximumChannelCount()
        except AttributeError:
            max_channels = preferred.channelCount()
        return 2 if max_channels >= 2 else 1

    @staticmethod
    def _make_format(sample_rate: int, channels: int) -> QAudioFormat:
        audio_format = QAudioFormat()
        audio_format.setSampleRate(sample_rate)
        audio_format.setChannelCount(channels)
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        return audio_format

    @staticmethod
    def _bytes_to_float(data: bytes, audio_format: QAudioFormat) -> np.ndarray:
        sample_format = audio_format.sampleFormat()
        if sample_format == QAudioFormat.SampleFormat.Int16:
            samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        elif sample_format == QAudioFormat.SampleFormat.Int32:
            samples = np.frombuffer(data, dtype="<i4").astype(np.float32) / 2_147_483_648.0
        elif sample_format == QAudioFormat.SampleFormat.UInt8:
            samples = (np.frombuffer(data, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif sample_format == QAudioFormat.SampleFormat.Float:
            samples = np.frombuffer(data, dtype="<f4").astype(np.float32)
        else:
            return np.empty(0, dtype=np.float32)

        channels = max(1, audio_format.channelCount())
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        return np.clip(samples, -1.0, 1.0).astype(np.float32, copy=False)

    @staticmethod
    def _float_to_bytes(samples: np.ndarray, audio_format: QAudioFormat) -> bytes:
        mono = np.clip(samples, -1.0, 1.0).astype(np.float32, copy=False)
        channels = max(1, audio_format.channelCount())
        if channels > 1:
            mono = np.repeat(mono[:, np.newaxis], channels, axis=1).reshape(-1)

        sample_format = audio_format.sampleFormat()
        if sample_format == QAudioFormat.SampleFormat.Int16:
            return (mono * 32767.0).astype("<i2").tobytes()
        if sample_format == QAudioFormat.SampleFormat.Int32:
            return (mono * 2_147_483_647.0).astype("<i4").tobytes()
        if sample_format == QAudioFormat.SampleFormat.UInt8:
            return ((mono * 127.0) + 128.0).astype(np.uint8).tobytes()
        if sample_format == QAudioFormat.SampleFormat.Float:
            return mono.astype("<f4").tobytes()
        return (mono * 32767.0).astype("<i2").tobytes()


def _resample_linear(samples: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate or samples.size == 0:
        return samples.astype(np.float32, copy=False)

    duration = samples.size / float(source_rate)
    target_size = max(1, int(round(duration * target_rate)))
    source_positions = np.linspace(0.0, samples.size - 1, num=samples.size, dtype=np.float64)
    target_positions = np.linspace(0.0, samples.size - 1, num=target_size, dtype=np.float64)
    return np.interp(target_positions, source_positions, samples).astype(np.float32)
