from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PySide6.QtCore import QStandardPaths, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollBar,
    QSplitter,
    QStatusBar,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from .audio import AudioEngine
from .storage import (
    AudioClip,
    SessionStore,
    Slide,
    clone_clips,
    format_duration,
    mix_clips,
    resample_linear,
    safe_file_stem,
)
from .waveform import WaveformWidget


RECORDING_PREVIEW_CLIP_ID = -1


@dataclass
class UndoState:
    clips: list[AudioClip]
    selection: tuple[int, int]
    cursor: int
    label: str


class MainWindow(QMainWindow):
    MAX_UNDO_STATES = 50

    def __init__(self, session_dir: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle("Slide Recorder")

        self.store = SessionStore.load(session_dir or default_session_dir())
        self.audio = AudioEngine(sample_rate=self.store.sample_rate, parent=self)

        self._selected_index = 0
        self._updating_slide_list = False
        self._recording = False
        self._mic_ready = False
        self._recording_cursor: int | None = None
        self._recording_source_clips: list[AudioClip] | None = None
        self._recording_preview_size = 0
        self._last_recording_preview_time = 0.0
        self._playback_origin_sample = 0
        self._playback_active = False
        self._undo_stacks: dict[int, list[UndoState]] = {}

        self._build_actions()
        self._build_ui()
        self._load_input_devices()
        self._populate_slide_list()
        self._select_slide(0)
        self._start_microphone()

        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self._tick)
        self.timer.start()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._recording:
            response = QMessageBox.question(
                self,
                "Save current take?",
                "Recording is still running. Stop and save this take before closing?",
            )
            if response == QMessageBox.StandardButton.Yes:
                self._stop_recording(save=True)
            else:
                self.audio.discard_recording()

        self.audio.stop_playback()
        self.audio.stop()
        self.store.save()
        event.accept()

    def _build_actions(self) -> None:
        open_session = QAction("Open Session Folder...", self)
        open_session.triggered.connect(self._open_session_folder)

        export_current = QAction("Export Current Slide...", self)
        export_current.triggered.connect(self._export_current_slide)

        export_all = QAction("Export All Slides...", self)
        export_all.triggered.connect(self._export_all_slides)

        self.undo_action = QAction("Undo", self)
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.triggered.connect(self._undo_current_slide)

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)

        edit_menu = self.menuBar().addMenu("Edit")
        edit_menu.addAction(self.undo_action)

        file_menu = self.menuBar().addMenu("File")
        file_menu.addAction(open_session)
        file_menu.addSeparator()
        file_menu.addAction(export_current)
        file_menu.addAction(export_all)
        file_menu.addSeparator()
        file_menu.addAction(quit_action)

    def _build_ui(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_slide_panel())
        splitter.addWidget(self._build_editor_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([280, 900])
        self.setCentralWidget(splitter)

        status = QStatusBar()
        self.setStatusBar(status)
        status.showMessage(f"Session: {self.store.directory}")

    def _build_slide_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)

        title = QLabel("Slides")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        layout.addWidget(title)

        self.slide_list = QListWidget()
        self.slide_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.slide_list.currentRowChanged.connect(self._select_slide)
        self.slide_list.itemChanged.connect(self._slide_item_changed)
        self.slide_list.itemSelectionChanged.connect(self._update_button_states)
        layout.addWidget(self.slide_list, 1)

        buttons = QHBoxLayout()
        self.add_slide_button = QPushButton("Add")
        self.add_slide_button.clicked.connect(self._add_slide)
        self.remove_slide_button = QPushButton("Remove")
        self.remove_slide_button.clicked.connect(self._remove_selected_slides)
        buttons.addWidget(self.add_slide_button)
        buttons.addWidget(self.remove_slide_button)
        layout.addLayout(buttons)

        return panel

    def _build_editor_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)

        self.slide_title = QLabel()
        self.slide_title.setStyleSheet("font-size: 22px; font-weight: 600;")
        layout.addWidget(self.slide_title)

        self.file_label = QLabel()
        self.file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.file_label)

        self.waveform = WaveformWidget()
        self.waveform.selectionChanged.connect(self._selection_changed)
        self.waveform.cursorChanged.connect(self._cursor_changed)
        self.waveform.playSelectionRequested.connect(self._play_selection)
        self.waveform.playFromCursorRequested.connect(self._play_current_slide)
        self.waveform.trimSelectionRequested.connect(self._trim_to_selection)
        self.waveform.cutSelectionRequested.connect(self._cut_selection)
        self.waveform.deleteRecordingRequested.connect(self._delete_recording)
        self.waveform.clipSelected.connect(self._clip_selected)
        self.waveform.clipMoved.connect(self._move_clip)
        self.waveform.deleteClipRequested.connect(self._delete_clip)
        self.waveform.clipPriorityRequested.connect(self._change_clip_priority)
        self.waveform.viewportChanged.connect(self._waveform_viewport_changed)
        layout.addWidget(self.waveform, 1)

        viewport_controls = QHBoxLayout()
        self.waveform_scrollbar = QScrollBar(Qt.Orientation.Horizontal)
        self.waveform_scrollbar.valueChanged.connect(self.waveform.set_view_start)
        viewport_controls.addWidget(self.waveform_scrollbar, 1)

        self.center_waveform_button = QPushButton("Center")
        self.center_waveform_button.clicked.connect(self._center_waveform)
        viewport_controls.addWidget(self.center_waveform_button)

        self.fit_waveform_button = QPushButton("Fit")
        self.fit_waveform_button.clicked.connect(self._fit_waveform)
        viewport_controls.addWidget(self.fit_waveform_button)

        self.default_zoom_button = QPushButton("Default")
        self.default_zoom_button.clicked.connect(self._reset_waveform_zoom)
        viewport_controls.addWidget(self.default_zoom_button)
        layout.addLayout(viewport_controls)

        self.selection_label = QLabel("Selection: none")
        layout.addWidget(self.selection_label)

        layout.addWidget(self._build_transport_box())
        layout.addWidget(self._build_mic_box())

        return panel

    def _build_transport_box(self) -> QWidget:
        box = QGroupBox("Recording and Playback")
        layout = QHBoxLayout(box)

        self.record_button = QPushButton("Record")
        self.record_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.record_button.clicked.connect(self._toggle_recording)
        layout.addWidget(self.record_button)

        self.play_button = QPushButton("Play")
        self.play_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.play_button.clicked.connect(self._play_current_slide)
        layout.addWidget(self.play_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop))
        self.stop_button.clicked.connect(self._stop_playback)
        layout.addWidget(self.stop_button)

        self.duration_label = QLabel("Duration: 00:00.00")
        layout.addWidget(self.duration_label)
        layout.addStretch(1)

        return box

    def _build_mic_box(self) -> QWidget:
        box = QGroupBox("Microphone")
        layout = QGridLayout(box)

        layout.addWidget(QLabel("Input"), 0, 0)
        self.device_combo = QComboBox()
        self.device_combo.currentIndexChanged.connect(self._device_changed)
        layout.addWidget(self.device_combo, 0, 1)

        self.mic_status = QLabel("Starting microphone...")
        layout.addWidget(self.mic_status, 1, 0)

        self.level_meter = QProgressBar()
        self.level_meter.setRange(0, 100)
        self.level_meter.setTextVisible(False)
        layout.addWidget(self.level_meter, 1, 1)

        self.confirm_delete_checkbox = QCheckBox("Ask before deleting")
        self.confirm_delete_checkbox.setChecked(self.store.ask_confirm_delete)
        self.confirm_delete_checkbox.toggled.connect(self._confirm_delete_changed)
        layout.addWidget(self.confirm_delete_checkbox, 2, 0, 1, 2)

        return box

    def _load_input_devices(self) -> None:
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        self.device_combo.addItem("System Default", None)
        for device in AudioEngine.input_devices():
            self.device_combo.addItem(f"{device.name} ({device.channels} ch)", device.index)
        self.device_combo.blockSignals(False)

    def _populate_slide_list(self) -> None:
        self._updating_slide_list = True
        self.slide_list.clear()
        for slide in self.store.slides:
            item = QListWidgetItem(self._slide_item_text(slide))
            item.setData(Qt.ItemDataRole.UserRole, slide.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.slide_list.addItem(item)
        self._updating_slide_list = False

    def _select_slide(self, index: int) -> None:
        if index < 0 or index >= len(self.store.slides):
            return
        if self._recording:
            self.slide_list.setCurrentRow(self._selected_index)
            return

        self._stop_playback(show_status=False)
        self._selected_index = index
        slide = self.current_slide
        self.slide_title.setText(slide.title)
        self.file_label.setText(f"File: {self.store.audio_path(slide)}")
        self._refresh_waveform_for_current_slide(cursor=0)
        self._update_button_states()

    @property
    def current_slide(self) -> Slide:
        return self.store.slides[self._selected_index]

    def _slide_item_text(self, slide: Slide) -> str:
        duration = format_duration(slide.samples, self.store.sample_rate)
        suffix = duration if slide.has_audio else "no audio"
        return f"{slide.title}   {suffix}"

    def _slide_item_changed(self, item: QListWidgetItem) -> None:
        if self._updating_slide_list:
            return
        slide_id = int(item.data(Qt.ItemDataRole.UserRole))
        slide = next((candidate for candidate in self.store.slides if candidate.id == slide_id), None)
        if slide is None:
            return
        title = item.text().strip()
        if "   " in title:
            title = title.split("   ", 1)[0].strip()
        slide.title = title or f"Slide {self.store.slides.index(slide) + 1}"
        self.store.save()
        self._refresh_slide_item(slide)
        if slide is self.current_slide:
            self.slide_title.setText(slide.title)

    def _refresh_slide_item(self, slide: Slide) -> None:
        for row in range(self.slide_list.count()):
            item = self.slide_list.item(row)
            if int(item.data(Qt.ItemDataRole.UserRole)) == slide.id:
                self._updating_slide_list = True
                item.setText(self._slide_item_text(slide))
                self._updating_slide_list = False
                break

    def _refresh_waveform_for_current_slide(
        self,
        *,
        selected_clip_id: int | None = None,
        selection: tuple[int, int] | None = None,
        cursor: int | None = None,
    ) -> None:
        slide = self.current_slide
        mixed = slide.samples
        self.waveform.set_audio(mixed, self.store.sample_rate)
        self.waveform.set_clips(slide.clips)
        if selection is not None:
            self.waveform.set_selection(*selection)
        if cursor is not None:
            self.waveform.set_cursor(cursor)
        if selected_clip_id is not None:
            self.waveform.set_selected_clip(selected_clip_id)
        if hasattr(self, "duration_label"):
            self.duration_label.setText(f"Duration: {format_duration(mixed, self.store.sample_rate)}")

    def _add_slide(self) -> None:
        slide = self.store.add_slide()
        self._undo_stacks[slide.id] = []
        self._populate_slide_list()
        self.slide_list.setCurrentRow(self.store.slides.index(slide))

    def _remove_selected_slides(self) -> None:
        if self._recording:
            return

        selected_ids = self._selected_slide_ids()
        if not selected_ids:
            selected_ids = [self.current_slide.id]

        selected_slides = [slide for slide in self.store.slides if slide.id in selected_ids]
        if not selected_slides:
            return

        if self.store.ask_confirm_delete:
            if len(selected_slides) == 1:
                prompt = f"Remove {selected_slides[0].title} and its audio file?"
            else:
                prompt = f"Remove {len(selected_slides)} selected slides and their audio files?"
            response = QMessageBox.question(self, "Remove slide?", prompt)
            if response != QMessageBox.StandardButton.Yes:
                return

        selected_rows = sorted(
            row
            for row in range(self.slide_list.count())
            if int(self.slide_list.item(row).data(Qt.ItemDataRole.UserRole)) in selected_ids
        )
        next_index = selected_rows[0] if selected_rows else self._selected_index

        for slide in selected_slides:
            self._undo_stacks.pop(slide.id, None)
            self.store.remove_slide(slide)

        self._selected_index = max(0, min(next_index, len(self.store.slides) - 1))
        self._populate_slide_list()
        self.slide_list.setCurrentRow(self._selected_index)

    def _selected_slide_ids(self) -> list[int]:
        return [
            int(item.data(Qt.ItemDataRole.UserRole))
            for item in self.slide_list.selectedItems()
            if item.data(Qt.ItemDataRole.UserRole) is not None
        ]

    def _start_microphone(self) -> None:
        device = self.device_combo.currentData()
        try:
            self.audio.start(input_device=device, sample_rate=self.store.sample_rate)
            self._mic_ready = True
            self.mic_status.setText(f"Mic on at {self.audio.sample_rate} Hz")
            if not self.store.has_any_audio() and self.store.sample_rate != self.audio.sample_rate:
                self.store.sample_rate = self.audio.sample_rate
                self.store.save()
                self._refresh_waveform_for_current_slide()
        except Exception as exc:
            self._mic_ready = False
            self.mic_status.setText(f"Mic unavailable: {exc}")
        self._update_button_states()

    def _device_changed(self) -> None:
        if self._recording:
            return
        device = self.device_combo.currentData()
        try:
            self.audio.restart(input_device=device, sample_rate=self.store.sample_rate)
            self._mic_ready = True
            self.mic_status.setText(f"Mic on at {self.audio.sample_rate} Hz")
        except Exception as exc:
            self._mic_ready = False
            self.mic_status.setText(f"Mic unavailable: {exc}")
        self._update_button_states()

    def _toggle_recording(self) -> None:
        if self._recording:
            self._stop_recording(save=True)
            return

        slide = self.current_slide
        cursor = self.waveform.cursor_sample()

        if not self._mic_ready:
            self._start_microphone()
        if not self._mic_ready:
            QMessageBox.warning(self, "Microphone unavailable", self.mic_status.text())
            return

        self.audio.stop_playback()
        self.waveform.set_playhead(None)
        self._playback_active = False
        self._recording_cursor = cursor
        self._recording_source_clips = clone_clips(slide.clips)
        self._recording_preview_size = 0
        self._last_recording_preview_time = 0.0
        self.audio.start_recording()
        self._recording = True
        composed, preview_clips, start = self._compose_recording_preview(np.empty(0, dtype=np.float32))
        self.waveform.set_audio(composed, self.store.sample_rate)
        self.waveform.set_clips(preview_clips)
        self.waveform.set_selected_clip(None)
        self.waveform.set_cursor(start)
        self.waveform.set_playhead(start)
        self._sync_record_button_label()
        self.statusBar().showMessage(f"Recording new clip from {format_seconds(cursor / self.store.sample_rate)}...")
        self._update_button_states()

    def _stop_recording(self, save: bool) -> None:
        samples = self.audio.stop_recording()
        cursor = self._recording_cursor
        source_clips = self._recording_source_clips
        self._recording_cursor = None
        self._recording_source_clips = None
        self._recording_preview_size = 0
        self._last_recording_preview_time = 0.0
        self._recording = False

        if save and samples.size:
            if self.audio.sample_rate != self.store.sample_rate:
                samples = resample_linear(samples, self.audio.sample_rate, self.store.sample_rate)
            start = max(0, int(cursor or 0))
            self._push_undo_state(
                "record",
                clips=source_clips if source_clips is not None else self.current_slide.clips,
                selection=(0, 0),
                cursor=start,
            )
            clip = self.store.add_clip(self.current_slide, start, samples)
            self._refresh_waveform_for_current_slide(selected_clip_id=clip.id, cursor=clip.end_sample)
            self._refresh_slide_item(self.current_slide)
            self.statusBar().showMessage(f"Recorded new clip: {format_seconds(samples.size / self.store.sample_rate)}.")
        else:
            self._refresh_waveform_for_current_slide()
            self.statusBar().showMessage("Recording discarded.")

        self._sync_record_button_label()
        self._update_button_states()

    def _play_current_slide(self) -> None:
        if self._recording or self.audio.is_playing:
            return
        slide = self.current_slide
        if not slide.has_audio:
            return
        start = self.waveform.cursor_sample()
        if start >= slide.samples.size:
            return
        self._playback_origin_sample = start
        self.waveform.set_playhead(start)
        self.audio.play(slide.samples[start:], self.store.sample_rate)
        self._playback_active = self.audio.is_playing
        if self._playback_active:
            self.statusBar().showMessage(f"Playing {slide.title}")
        else:
            self.waveform.set_playhead(None)
        self._update_button_states()

    def _play_selection(self) -> None:
        if self._recording or self.audio.is_playing or not self.waveform.has_selection():
            return
        start, end = self.waveform.selection()
        samples = self.current_slide.samples[start:end]
        if samples.size:
            self._playback_origin_sample = start
            self.waveform.set_playhead(start)
            self.audio.play(samples, self.store.sample_rate)
            self._playback_active = self.audio.is_playing
            if self._playback_active:
                self.statusBar().showMessage("Playing selection")
            else:
                self.waveform.set_playhead(None)
        self._update_button_states()

    def _stop_playback(self, show_status: bool = True) -> None:
        self.audio.stop_playback()
        self._playback_active = False
        self.waveform.set_playhead(None)
        if hasattr(self, "duration_label"):
            self.duration_label.setText(
                f"Duration: {format_duration(self.current_slide.samples, self.store.sample_rate)}"
            )
        if show_status:
            self.statusBar().showMessage("Playback stopped.")
        self._update_button_states()

    def _trim_to_selection(self) -> None:
        if self._recording or not self.waveform.has_selection():
            return
        start, end = self.waveform.selection()
        self._push_undo_state("trim")
        slide = self.current_slide
        self.store.replace_slide_clips(slide, self._clips_trimmed_to_range(slide.clips, start, end))
        self._save_current_audio_edit("Trimmed recording.")

    def _cut_selection(self, confirm: bool = True) -> None:
        if self._recording or not self.waveform.has_selection():
            return
        start, end = self.waveform.selection()
        if confirm and self.store.ask_confirm_delete:
            response = QMessageBox.question(
                self,
                "Cut selected audio?",
                f"Delete {((end - start) / self.store.sample_rate):.3f}s from {self.current_slide.title}?",
            )
            if response != QMessageBox.StandardButton.Yes:
                return
        self._push_undo_state("cut")
        slide = self.current_slide
        self.store.replace_slide_clips(slide, self._clips_with_range_cut(slide.clips, start, end))
        self._save_current_audio_edit("Cut selection.")

    def _delete_recording(self) -> None:
        if self._recording or not self.current_slide.has_audio:
            return
        if self.store.ask_confirm_delete:
            response = QMessageBox.question(
                self,
                "Delete recording?",
                f"Delete the recording for {self.current_slide.title}?",
            )
            if response != QMessageBox.StandardButton.Yes:
                return
        self._push_undo_state("delete")
        self.store.replace_slide_clips(self.current_slide, [])
        self._save_current_audio_edit("Deleted recording.")

    def _delete_clip(self, clip_id: int) -> None:
        if self._recording or self.audio.is_playing:
            return
        clip = self.store.clip_by_id(self.current_slide, clip_id)
        if clip is None:
            return
        self._push_undo_state("delete clip")
        self.store.remove_clip(self.current_slide, clip_id)
        self._refresh_waveform_for_current_slide(cursor=min(clip.start_sample, self.current_slide.samples.size))
        self._refresh_slide_item(self.current_slide)
        self._update_button_states()
        self.statusBar().showMessage("Deleted clip.")

    def _move_clip(self, clip_id: int, start_sample: int) -> None:
        if self._recording or self.audio.is_playing:
            return
        clip = self.store.clip_by_id(self.current_slide, clip_id)
        if clip is None or clip.start_sample == start_sample:
            self._refresh_waveform_for_current_slide(selected_clip_id=clip_id if clip is not None else None)
            return
        self._push_undo_state("move clip")
        self.store.move_clip(self.current_slide, clip_id, start_sample)
        moved = self.store.clip_by_id(self.current_slide, clip_id)
        cursor = moved.start_sample if moved is not None else start_sample
        self._refresh_waveform_for_current_slide(selected_clip_id=clip_id, cursor=cursor)
        self._refresh_slide_item(self.current_slide)
        self._update_button_states()
        self.statusBar().showMessage("Moved clip.")

    def _change_clip_priority(self, clip_id: int, direction: str) -> None:
        if self._recording or self.audio.is_playing:
            return
        clip = self.store.clip_by_id(self.current_slide, clip_id)
        if clip is None:
            return

        ordered = sorted(self.current_slide.clips, key=lambda candidate: (candidate.layer, candidate.id))
        current_index = next((index for index, candidate in enumerate(ordered) if candidate.id == clip_id), None)
        if current_index is None:
            return
        if direction == "front":
            if current_index == len(ordered) - 1:
                return
            label = "bring clip to front"
            changed = self.store.bring_clip_to_front
            message = "Brought clip to front."
        elif direction == "back":
            if current_index == 0:
                return
            label = "send clip to back"
            changed = self.store.send_clip_to_back
            message = "Sent clip to back."
        else:
            return

        self._push_undo_state(label)
        if changed(self.current_slide, clip_id):
            cursor = min(self.waveform.cursor_sample(), self.current_slide.samples.size)
            self._refresh_waveform_for_current_slide(selected_clip_id=clip_id, cursor=cursor)
            self._refresh_slide_item(self.current_slide)
            self._update_button_states()
            self.statusBar().showMessage(message)

    def _save_current_audio_edit(self, message: str) -> None:
        self.audio.stop_playback()
        self.store.save_slide_audio(self.current_slide)
        self._refresh_waveform_for_current_slide(
            cursor=min(self.waveform.cursor_sample(), self.current_slide.samples.size)
        )
        self._refresh_slide_item(self.current_slide)
        self._update_button_states()
        self.statusBar().showMessage(message)

    def _push_undo_state(
        self,
        label: str,
        clips: list[AudioClip] | None = None,
        selection: tuple[int, int] | None = None,
        cursor: int | None = None,
    ) -> None:
        slide = self.current_slide
        stack = self._undo_stacks.setdefault(slide.id, [])
        stack.append(
            UndoState(
                clips=clone_clips(clips if clips is not None else slide.clips),
                selection=selection if selection is not None else self.waveform.selection(),
                cursor=cursor if cursor is not None else self.waveform.cursor_sample(),
                label=label,
            )
        )
        if len(stack) > self.MAX_UNDO_STATES:
            del stack[0 : len(stack) - self.MAX_UNDO_STATES]
        self._update_undo_action()

    def _undo_current_slide(self) -> None:
        if self._recording or self.audio.is_playing:
            return
        stack = self._undo_stacks.get(self.current_slide.id, [])
        if not stack:
            return

        state = stack.pop()
        self.audio.stop_playback()
        self.store.replace_slide_clips(self.current_slide, state.clips)
        self._refresh_waveform_for_current_slide(cursor=state.cursor)
        start, end = state.selection
        if end > start and self.current_slide.samples.size:
            self.waveform.set_selection(start, min(end, self.current_slide.samples.size))
        else:
            self.waveform.set_cursor(state.cursor)
        self._refresh_slide_item(self.current_slide)
        self._update_button_states()
        self.statusBar().showMessage(f"Undid {state.label}.")

    def _clips_trimmed_to_range(self, clips: list[AudioClip], start: int, end: int) -> list[AudioClip]:
        trimmed: list[AudioClip] = []
        for clip in clips:
            overlap_start = max(start, clip.start_sample)
            overlap_end = min(end, clip.end_sample)
            if overlap_end <= overlap_start:
                continue
            local_start = overlap_start - clip.start_sample
            local_end = overlap_end - clip.start_sample
            trimmed.append(
                AudioClip(
                    id=clip.id,
                    start_sample=overlap_start - start,
                    file_name=clip.file_name,
                    layer=clip.layer,
                    samples=clip.samples[local_start:local_end].copy(),
                )
            )
        return trimmed

    def _clips_with_range_cut(self, clips: list[AudioClip], start: int, end: int) -> list[AudioClip]:
        duration = max(0, end - start)
        if duration == 0:
            return clone_clips(clips)

        edited: list[AudioClip] = []
        for clip in clips:
            if clip.end_sample <= start:
                edited.append(
                    AudioClip(
                        id=clip.id,
                        start_sample=clip.start_sample,
                        file_name=clip.file_name,
                        layer=clip.layer,
                        samples=clip.samples.copy(),
                    )
                )
                continue

            if clip.start_sample >= end:
                edited.append(
                    AudioClip(
                        id=clip.id,
                        start_sample=max(0, clip.start_sample - duration),
                        file_name=clip.file_name,
                        layer=clip.layer,
                        samples=clip.samples.copy(),
                    )
                )
                continue

            if clip.start_sample < start:
                left_size = start - clip.start_sample
                edited.append(
                    AudioClip(
                        id=clip.id,
                        start_sample=clip.start_sample,
                        file_name=clip.file_name,
                        layer=clip.layer,
                        samples=clip.samples[:left_size].copy(),
                    )
                )

            if clip.end_sample > end:
                local_start = end - clip.start_sample
                edited.append(
                    AudioClip(
                        id=clip.id,
                        start_sample=max(0, max(clip.start_sample, end) - duration),
                        file_name=clip.file_name,
                        layer=clip.layer,
                        samples=clip.samples[local_start:].copy(),
                    )
                )

        return edited

    def _selection_changed(self, start: int, end: int) -> None:
        if end > start:
            duration = (end - start) / float(self.store.sample_rate)
            self.selection_label.setText(
                f"Cursor: {format_seconds(self.waveform.cursor_sample() / self.store.sample_rate)} | "
                f"Selection: {start / self.store.sample_rate:.3f}s - "
                f"{end / self.store.sample_rate:.3f}s ({duration:.3f}s)"
            )
        else:
            self.selection_label.setText(
                f"Cursor: {format_seconds(self.waveform.cursor_sample() / self.store.sample_rate)}"
            )
        self._sync_record_button_label()
        self._update_button_states()

    def _cursor_changed(self, sample: int) -> None:
        if not self.waveform.has_selection():
            self.selection_label.setText(f"Cursor: {format_seconds(sample / self.store.sample_rate)}")
        self._sync_record_button_label()

    def _clip_selected(self, clip_id: int) -> None:
        if clip_id < 0:
            if not self.waveform.has_selection():
                self.selection_label.setText(
                    f"Cursor: {format_seconds(self.waveform.cursor_sample() / self.store.sample_rate)}"
                )
            return
        clip = self.store.clip_by_id(self.current_slide, clip_id)
        if clip is None:
            return
        self.selection_label.setText(
            f"Cursor: {format_seconds(self.waveform.cursor_sample() / self.store.sample_rate)} | "
            f"Clip: {format_seconds(clip.start_sample / self.store.sample_rate)} - "
            f"{format_seconds(clip.end_sample / self.store.sample_rate)}"
        )

    def _confirm_delete_changed(self, checked: bool) -> None:
        self.store.ask_confirm_delete = checked
        self.store.save()

    def _waveform_viewport_changed(self, value: int, maximum: int, page_step: int) -> None:
        self.waveform_scrollbar.blockSignals(True)
        try:
            self.waveform_scrollbar.setRange(0, maximum)
            self.waveform_scrollbar.setPageStep(page_step)
            self.waveform_scrollbar.setSingleStep(max(1, page_step // 12))
            self.waveform_scrollbar.setValue(value)
        finally:
            self.waveform_scrollbar.blockSignals(False)
        self._update_viewport_controls()

    def _fit_waveform(self) -> None:
        self.waveform.fit_to_window()

    def _center_waveform(self) -> None:
        self.waveform.center_on_selection_or_playhead()

    def _reset_waveform_zoom(self) -> None:
        self.waveform.reset_timeline_scale()

    def _update_button_states(self) -> None:
        slide_has_audio = self.current_slide.has_audio
        is_playing = self.audio.is_playing

        self._sync_record_button_label()
        self._update_undo_action()

        self.record_button.setEnabled(not is_playing or self._recording)
        self.play_button.setEnabled(slide_has_audio and not self._recording and not is_playing)
        self.stop_button.setEnabled(is_playing and not self._recording)
        self.add_slide_button.setEnabled(not self._recording)
        has_selected_slides = bool(self._selected_slide_ids())
        self.remove_slide_button.setEnabled(not self._recording and has_selected_slides and len(self.store.slides) > 1)
        self.slide_list.setEnabled(not self._recording and not is_playing)
        self.device_combo.setEnabled(not self._recording)
        self.waveform.setEnabled(not self._recording and not is_playing)
        self.confirm_delete_checkbox.setEnabled(not self._recording)
        self._update_viewport_controls()

    def _update_undo_action(self) -> None:
        if not hasattr(self, "undo_action"):
            return
        stack = self._undo_stacks.get(self.current_slide.id, [])
        self.undo_action.setEnabled(bool(stack) and not self._recording and not self.audio.is_playing)
        if stack:
            self.undo_action.setText(f"Undo {stack[-1].label.title()}")
        else:
            self.undo_action.setText("Undo")

    def _update_viewport_controls(self) -> None:
        if not hasattr(self, "waveform_scrollbar"):
            return
        has_audio = self.current_slide.has_audio
        self.waveform_scrollbar.setEnabled(has_audio and not self._recording and self.waveform_scrollbar.maximum() > 0)
        self.center_waveform_button.setEnabled(has_audio and not self._recording)
        self.fit_waveform_button.setEnabled(has_audio and not self._recording)
        self.default_zoom_button.setEnabled(has_audio and not self._recording)

    def _tick(self) -> None:
        level = self.audio.level
        self.level_meter.setValue(min(100, int(level * 300)))

        if self._recording:
            self._update_recording_preview()
            seconds = self.audio.recorded_seconds
            self.duration_label.setText(f"Recording: {format_seconds(seconds)}")
        elif self.audio.is_playing:
            elapsed = self.audio.playback_elapsed_seconds
            total = self.audio.playback_duration_seconds
            playhead = self._playback_origin_sample + int(round(elapsed * self.store.sample_rate))
            self.waveform.set_playhead(playhead)
            self.duration_label.setText(f"Playback: {format_seconds(elapsed)} / {format_seconds(total)}")
            self.stop_button.setEnabled(True)
        elif self._playback_active:
            self._playback_active = False
            self.waveform.set_playhead(None)
            self.duration_label.setText(
                f"Duration: {format_duration(self.current_slide.samples, self.store.sample_rate)}"
            )
            self._update_button_states()
        elif not self.audio.is_playing:
            self.stop_button.setEnabled(False)

        status = self.audio.last_status
        if status and self._mic_ready:
            self.mic_status.setText(f"Mic on at {self.audio.sample_rate} Hz ({status})")

    def _update_recording_preview(self) -> None:
        now = time.monotonic()
        if now - self._last_recording_preview_time < 0.15:
            return
        self._last_recording_preview_time = now
        preview = self.audio.recording_snapshot()
        if preview.size == self._recording_preview_size:
            return
        self._recording_preview_size = preview.size
        if self.audio.sample_rate != self.store.sample_rate:
            preview = resample_linear(preview, self.audio.sample_rate, self.store.sample_rate)
        composed, preview_clips, start = self._compose_recording_preview(preview)
        self.waveform.set_audio(composed, self.store.sample_rate)
        self.waveform.set_clips(preview_clips)
        if preview.size:
            self.waveform.set_selected_clip(-1)
        self.waveform.set_cursor(start + preview.size)
        self.waveform.set_playhead(start + preview.size)

    def _compose_recording_preview(self, preview: np.ndarray) -> tuple[np.ndarray, list[AudioClip], int]:
        source_clips = clone_clips(self._recording_source_clips or [])
        start = max(0, int(self._recording_cursor or 0))
        if preview.size:
            source_clips.append(
                AudioClip(
                    id=RECORDING_PREVIEW_CLIP_ID,
                    start_sample=start,
                    file_name="recording_preview.wav",
                    layer=max((clip.layer for clip in source_clips), default=0) + 1,
                    samples=preview.astype(np.float32, copy=True),
                )
            )
        return mix_clips(source_clips, priority_clip_id=RECORDING_PREVIEW_CLIP_ID), source_clips, start

    def _sync_record_button_label(self) -> None:
        if not hasattr(self, "record_button"):
            return
        if self._recording:
            self.record_button.setText("Stop Recording")
            self.record_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop))
            return
        self.record_button.setText("Record New Clip" if self.current_slide.has_audio else "Record")
        self.record_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))

    def _open_session_folder(self) -> None:
        if self._recording:
            return
        directory = QFileDialog.getExistingDirectory(self, "Open Session Folder", str(self.store.directory))
        if not directory:
            return
        self.audio.stop_playback()
        self.store = SessionStore.load(Path(directory))
        self._selected_index = 0
        self._undo_stacks = {}
        self.confirm_delete_checkbox.setChecked(self.store.ask_confirm_delete)
        self._populate_slide_list()
        self.slide_list.setCurrentRow(0)
        self._device_changed()
        self.statusBar().showMessage(f"Session: {self.store.directory}")

    def _export_current_slide(self) -> None:
        if self._recording:
            return
        slide = self.current_slide
        if not slide.has_audio:
            QMessageBox.information(self, "No recording", "The selected slide does not have a recording.")
            return
        default_name = f"{safe_file_stem(slide.title)}.wav"
        destination, _ = QFileDialog.getSaveFileName(
            self,
            "Export Current Slide",
            str(self.store.directory / default_name),
            "WAV audio (*.wav)",
        )
        if not destination:
            return
        self.store.export_slide(slide, Path(destination))
        self.statusBar().showMessage(f"Exported {destination}")

    def _export_all_slides(self) -> None:
        if self._recording:
            return
        destination, _ = QFileDialog.getSaveFileName(
            self,
            "Export All Slides",
            str(self.store.directory / "slide_voiceovers.zip"),
            "ZIP archive (*.zip)",
        )
        if not destination:
            return
        count = self.store.export_all_zip(Path(destination))
        QMessageBox.information(self, "Export complete", f"Exported {count} recorded slide audio files.")
        self.statusBar().showMessage(f"Exported {destination}")


def default_session_dir() -> Path:
    documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
    if documents:
        return Path(documents) / "Slide Recorder"
    return Path.home() / "Documents" / "Slide Recorder"


def format_seconds(seconds: float) -> str:
    minutes = int(seconds // 60)
    remainder = seconds - (minutes * 60)
    return f"{minutes:02d}:{remainder:05.2f}"


def run() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()
