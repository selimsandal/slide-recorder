from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QPen, QPolygon
from PySide6.QtWidgets import QMenu, QWidget

from .storage import format_duration


@dataclass
class WaveformClip:
    id: int
    start_sample: int
    layer: int
    samples: np.ndarray

    @property
    def end_sample(self) -> int:
        return self.start_sample + int(self.samples.size)


class WaveformWidget(QWidget):
    selectionChanged = Signal(int, int)
    viewportChanged = Signal(int, int, int)
    cursorChanged = Signal(int)
    clipSelected = Signal(int)
    clipMoved = Signal(int, int)
    deleteClipRequested = Signal(int)
    clipPriorityRequested = Signal(int, str)
    playSelectionRequested = Signal()
    playFromCursorRequested = Signal()
    trimSelectionRequested = Signal()
    cutSelectionRequested = Signal(bool)
    deleteRecordingRequested = Signal()
    DEFAULT_PIXELS_PER_SECOND = 100.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(220)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._samples = np.empty(0, dtype=np.float32)
        self._clips: list[WaveformClip] = []
        self._clip_lanes: dict[int, int] = {}
        self._lane_count = 1
        self._selected_clip_id: int | None = None
        self._sample_rate = 48_000
        self._selection_start = 0
        self._selection_end = 0
        self._drag_anchor = 0
        self._dragging = False
        self._drag_mode = "select"
        self._move_start = 0
        self._move_end = 0
        self._clip_drag_id: int | None = None
        self._clip_drag_anchor = 0
        self._clip_drag_start = 0
        self._cursor_sample = 0
        self._playhead_sample: int | None = None
        self._samples_per_pixel = self._default_samples_per_pixel()
        self._view_start_sample = 0

    def set_audio(self, samples: np.ndarray, sample_rate: int) -> None:
        self._samples = samples.astype(np.float32, copy=False)
        self._sample_rate = sample_rate
        self._playhead_sample = None
        self._samples_per_pixel = max(1.0, self._samples_per_pixel)
        self._cursor_sample = max(0, min(self._cursor_sample, self._timeline_size()))
        self._view_start_sample = self._clamp_view_start(self._view_start_sample)
        self.clear_selection(emit=True)
        self.cursorChanged.emit(self._cursor_sample)
        self._emit_viewport_changed()
        self.update()

    def set_clips(self, clips) -> None:
        self._clips = [
            WaveformClip(
                id=int(clip.id),
                start_sample=max(0, int(clip.start_sample)),
                layer=int(getattr(clip, "layer", clip.id)),
                samples=np.asarray(clip.samples, dtype=np.float32),
            )
            for clip in clips
            if getattr(clip, "samples", np.empty(0, dtype=np.float32)).size > 0
        ]
        self._clips.sort(key=lambda clip: (clip.start_sample, clip.layer, clip.id))
        self._assign_clip_lanes()
        if self._selected_clip_id is not None and self._clip_by_id(self._selected_clip_id) is None:
            self._selected_clip_id = None
            self.clipSelected.emit(-1)
        self._cursor_sample = max(0, min(self._cursor_sample, self._timeline_size()))
        self._view_start_sample = self._clamp_view_start(self._view_start_sample)
        self._emit_viewport_changed()
        self.update()

    def set_selected_clip(self, clip_id: int | None, emit: bool = True) -> None:
        if clip_id is not None and self._clip_by_id(clip_id) is None:
            clip_id = None
        if self._selected_clip_id == clip_id:
            return
        self._selected_clip_id = clip_id
        if clip_id is not None:
            self.clear_selection(emit=True)
        if emit:
            self.clipSelected.emit(-1 if clip_id is None else clip_id)
        self.update()

    def selected_clip_id(self) -> int | None:
        return self._selected_clip_id

    def fit_to_window(self) -> None:
        rect = self._content_rect()
        timeline_size = self._timeline_size()
        if timeline_size == 0 or rect.width() <= 0:
            return
        self._samples_per_pixel = max(1.0, timeline_size / float(rect.width()))
        self._view_start_sample = 0
        self._emit_viewport_changed()
        self.update()

    def reset_timeline_scale(self) -> None:
        self._samples_per_pixel = self._default_samples_per_pixel()
        self._view_start_sample = self._clamp_view_start(self._view_start_sample)
        self._emit_viewport_changed()
        self.update()

    def center_on_selection_or_playhead(self) -> None:
        if self.has_selection():
            center_sample = (self._selection_start + self._selection_end) // 2
        elif self._selected_clip_id is not None and self._clip_by_id(self._selected_clip_id) is not None:
            clip = self._clip_by_id(self._selected_clip_id)
            center_sample = (clip.start_sample + clip.end_sample) // 2
        elif self._playhead_sample is not None:
            center_sample = self._playhead_sample
        else:
            center_sample = 0
        self.center_on_sample(center_sample)

    def center_on_sample(self, sample: int) -> None:
        visible = self._visible_samples()
        self.set_view_start(int(sample) - visible // 2)

    def set_view_start(self, sample: int) -> None:
        clamped = self._clamp_view_start(sample)
        if clamped == self._view_start_sample:
            self._emit_viewport_changed()
            return
        self._view_start_sample = clamped
        self._emit_viewport_changed()
        self.update()

    def scrollbar_state(self) -> tuple[int, int, int]:
        page_step = max(1, self._visible_samples())
        maximum = max(0, self._timeline_size() - page_step)
        return self._view_start_sample, maximum, page_step

    def set_selection(self, start: int, end: int, emit: bool = True) -> None:
        timeline_size = self._timeline_size()
        start = max(0, min(int(start), timeline_size))
        end = max(0, min(int(end), timeline_size))
        if end < start:
            start, end = end, start
        self._selection_start = start
        self._selection_end = end
        if self.has_selection():
            if self._selected_clip_id is not None:
                self.clipSelected.emit(-1)
            self._selected_clip_id = None
        if emit:
            self.selectionChanged.emit(start, end)
        self.update()

    def selection(self) -> tuple[int, int]:
        return self._selection_start, self._selection_end

    def has_selection(self) -> bool:
        return self._selection_end > self._selection_start

    def clear_selection(self, emit: bool = True) -> None:
        self._selection_start = 0
        self._selection_end = 0
        if emit:
            self.selectionChanged.emit(0, 0)
        self.update()

    def cursor_sample(self) -> int:
        return self._cursor_sample

    def set_cursor(self, sample: int, emit: bool = True) -> None:
        self._cursor_sample = max(0, min(int(sample), self._timeline_size()))
        if emit:
            self.cursorChanged.emit(self._cursor_sample)
        self.update()

    def set_playhead(self, sample: int | None) -> None:
        if sample is None:
            self._playhead_sample = None
        else:
            self._playhead_sample = max(0, min(int(sample), self._timeline_size()))
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._view_start_sample = self._clamp_view_start(self._view_start_sample)
        self._emit_viewport_changed()

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        rect = self._content_rect()
        painter.fillRect(self.rect(), QColor("#f7f7f7"))
        self._draw_ruler(painter, rect)
        painter.fillRect(rect, QColor("#ffffff"))
        painter.setPen(QPen(QColor("#d7d7d7"), 1))
        painter.drawRect(rect)

        self._draw_recorded_region(painter, rect)

        center_y = rect.center().y()
        painter.setPen(QPen(QColor("#dddddd"), 1))
        painter.drawLine(rect.left(), center_y, rect.right(), center_y)

        if self._timeline_size() == 0:
            painter.setPen(QColor("#6b6b6b"))
            text = "No recording for this slide"
            metrics = QFontMetrics(painter.font())
            painter.drawText(rect.center().x() - metrics.horizontalAdvance(text) // 2, center_y, text)
            return

        painter.save()
        painter.setClipRect(rect)
        self._draw_selection(painter, rect)
        self._draw_waveform(painter, rect)
        self._draw_cursor(painter, rect)
        self._draw_playhead(painter, rect)
        painter.restore()
        self._draw_duration(painter, rect)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._timeline_size() == 0:
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._dragging = True
        self._drag_anchor = self._sample_at_x(event.position().x())
        clip_id = self._hit_clip_header(event.position().x(), event.position().y())
        if clip_id is not None:
            clip = self._clip_by_id(clip_id)
            if clip is not None:
                self._drag_mode = "clip"
                self._clip_drag_id = clip.id
                self._clip_drag_anchor = self._drag_anchor
                self._clip_drag_start = clip.start_sample
                self.set_selected_clip(clip.id)
                self.set_cursor(self._drag_anchor)
                return

        self.set_selected_clip(None)
        self.set_cursor(self._drag_anchor)
        self._drag_mode = self._hit_test(event.position().x())
        if self._drag_mode == "start":
            self.set_selection(self._drag_anchor, self._selection_end)
        elif self._drag_mode == "end":
            self.set_selection(self._selection_start, self._drag_anchor)
        elif self._drag_mode == "move":
            self._move_start = self._selection_start
            self._move_end = self._selection_end
        else:
            self._drag_mode = "select"
            self.set_selection(self._drag_anchor, self._drag_anchor)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._dragging:
            self._update_cursor(event.position().x(), event.position().y())
            return
        current = self._sample_at_x(event.position().x())
        if self._drag_mode == "clip":
            clip = self._clip_by_id(self._clip_drag_id)
            if clip is not None:
                delta = current - self._clip_drag_anchor
                clip.start_sample = max(0, self._clip_drag_start + delta)
                self.set_cursor(clip.start_sample)
                self._emit_viewport_changed()
                self.update()
        elif self._drag_mode == "start":
            self.set_selection(current, self._selection_end)
        elif self._drag_mode == "end":
            self.set_selection(self._selection_start, current)
        elif self._drag_mode == "move":
            width = self._move_end - self._move_start
            delta = current - self._drag_anchor
            start = max(0, min(self._timeline_size() - width, self._move_start + delta))
            self.set_selection(start, start + width)
        else:
            self.set_selection(self._drag_anchor, current)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        current = self._sample_at_x(event.position().x())
        if self._drag_mode == "clip":
            clip = self._clip_by_id(self._clip_drag_id)
            if clip is not None:
                delta = current - self._clip_drag_anchor
                new_start = max(0, self._clip_drag_start + delta)
                clip.start_sample = new_start
                self.set_cursor(new_start)
                if new_start != self._clip_drag_start:
                    self.clipMoved.emit(clip.id, new_start)
        elif self._drag_mode == "start":
            self.set_selection(current, self._selection_end)
        elif self._drag_mode == "end":
            self.set_selection(self._selection_start, current)
        elif self._drag_mode == "move":
            width = self._move_end - self._move_start
            delta = current - self._drag_anchor
            start = max(0, min(self._timeline_size() - width, self._move_start + delta))
            self.set_selection(start, start + width)
        else:
            self.set_selection(self._drag_anchor, current)
        self._dragging = False
        self._drag_mode = "select"
        self._clip_drag_id = None
        self._update_cursor(event.position().x(), event.position().y())

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clear_selection()
            self.set_selected_clip(None)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.has_selection():
            self.cutSelectionRequested.emit(False)
            event.accept()
            return
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self._selected_clip_id is not None:
            self.deleteClipRequested.emit(self._selected_clip_id)
            event.accept()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.has_selection():
            self.trimSelectionRequested.emit()
            event.accept()
            return
        if key == Qt.Key.Key_Space and self.has_selection():
            self.playSelectionRequested.emit()
            event.accept()
            return
        if key == Qt.Key.Key_Space:
            self.playFromCursorRequested.emit()
            event.accept()
            return
        if key == Qt.Key.Key_Escape and (self.has_selection() or self._selected_clip_id is not None):
            self.clear_selection()
            self.set_selected_clip(None)
            event.accept()
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:
        clips_under_mouse = self._clips_at_position(event.pos().x(), event.pos().y())
        clip_select_actions = {}
        clip_front_actions = {}

        menu = QMenu(self)
        if clips_under_mouse:
            clip_menu = menu.addMenu("Clips Under Mouse")
            for clip in clips_under_mouse:
                action = clip_menu.addAction(f"Select {self._clip_menu_label(clip, clips_under_mouse)}")
                clip_select_actions[action] = clip.id
            if len(clips_under_mouse) > 1:
                clip_menu.addSeparator()
                for clip in clips_under_mouse:
                    action = clip_menu.addAction(f"Bring {self._clip_menu_label(clip, clips_under_mouse)} to Front")
                    action.setEnabled(clip.id != clips_under_mouse[0].id)
                    clip_front_actions[action] = clip.id
            menu.addSeparator()

        play_selection = menu.addAction("Play Selection")
        trim_selection = menu.addAction("Trim to Selection")
        cut_selection = menu.addAction("Cut Selection")
        bring_clip_to_front = menu.addAction("Bring Selected Clip to Front")
        send_clip_to_back = menu.addAction("Send Selected Clip to Back")
        delete_clip = menu.addAction("Delete Selected Clip")
        menu.addSeparator()
        clear_selection = menu.addAction("Clear Selection")
        center_view = menu.addAction("Center View")
        fit_view = menu.addAction("Fit to Window")
        delete_recording = menu.addAction("Delete Recording")

        has_selection = self.has_selection()
        play_selection.setEnabled(has_selection)
        trim_selection.setEnabled(has_selection)
        cut_selection.setEnabled(has_selection)
        selected_clip = self._clip_by_id(self._selected_clip_id)
        bring_clip_to_front.setEnabled(selected_clip is not None and not self._clip_is_front(selected_clip.id))
        send_clip_to_back.setEnabled(selected_clip is not None and not self._clip_is_back(selected_clip.id))
        delete_clip.setEnabled(selected_clip is not None)
        clear_selection.setEnabled(has_selection)
        delete_recording.setEnabled(self._timeline_size() > 0)

        action = menu.exec(event.globalPos())
        if action in clip_select_actions:
            clip_id = clip_select_actions[action]
            self.set_selected_clip(clip_id)
            self.set_cursor(self._sample_at_x(event.pos().x()))
        elif action in clip_front_actions:
            self.clipPriorityRequested.emit(clip_front_actions[action], "front")
        elif action == play_selection:
            self.playSelectionRequested.emit()
        elif action == trim_selection:
            self.trimSelectionRequested.emit()
        elif action == cut_selection:
            self.cutSelectionRequested.emit(True)
        elif action == bring_clip_to_front and self._selected_clip_id is not None:
            self.clipPriorityRequested.emit(self._selected_clip_id, "front")
        elif action == send_clip_to_back and self._selected_clip_id is not None:
            self.clipPriorityRequested.emit(self._selected_clip_id, "back")
        elif action == delete_clip and self._selected_clip_id is not None:
            self.deleteClipRequested.emit(self._selected_clip_id)
        elif action == clear_selection:
            self.clear_selection()
            self.set_selected_clip(None)
        elif action == center_view:
            self.center_on_selection_or_playhead()
        elif action == fit_view:
            self.fit_to_window()
        elif action == delete_recording:
            self.deleteRecordingRequested.emit()

    def _draw_waveform(self, painter: QPainter, rect) -> None:
        width = max(1, rect.width())
        if self._timeline_size() == 0:
            return
        if self._clips:
            self._draw_clip_waveforms(painter, rect)
            return

        half_height = max(1, rect.height() // 2 - 12)
        painter.setPen(QPen(QColor("#2457a6"), 1))
        for x_offset in range(width):
            start = int(self._view_start_sample + x_offset * self._samples_per_pixel)
            if start >= self._samples.size:
                break
            end = int(self._view_start_sample + (x_offset + 1) * self._samples_per_pixel)
            end = max(start + 1, min(end, self._samples.size))
            window = self._samples[start:end]
            if window.size == 0:
                continue
            low = float(window.min())
            high = float(window.max())
            x = rect.left() + x_offset
            y1 = int(rect.center().y() - high * half_height)
            y2 = int(rect.center().y() - low * half_height)
            painter.drawLine(x, y1, x, y2)

    def _draw_clip_waveforms(self, painter: QPainter, rect) -> None:
        visible_start = self._view_start_sample
        visible_end = self._view_start_sample + self._visible_samples()
        for clip in sorted(self._clips, key=self._clip_priority):
            if clip.samples.size == 0 or clip.end_sample <= visible_start or clip.start_sample >= visible_end:
                continue
            lane_rect = self._clip_lane_rect(rect, clip)
            half_height = max(1, lane_rect.height() // 2 - 8)
            clip_left = max(lane_rect.left(), int(self._x_at_sample(clip.start_sample)))
            clip_right = min(lane_rect.right(), int(self._x_at_sample(clip.end_sample)))
            if clip_right <= clip_left:
                continue
            for x in range(clip_left, clip_right + 1):
                global_start = int(self._view_start_sample + (x - rect.left()) * self._samples_per_pixel)
                global_end = int(self._view_start_sample + (x + 1 - rect.left()) * self._samples_per_pixel)
                local_start = max(0, global_start - clip.start_sample)
                local_end = min(clip.samples.size, max(local_start + 1, global_end - clip.start_sample))
                if local_start >= clip.samples.size or local_end <= local_start:
                    continue
                window = clip.samples[local_start:local_end]
                low = float(window.min())
                high = float(window.max())
                y1 = int(lane_rect.center().y() - high * half_height)
                y2 = int(lane_rect.center().y() - low * half_height)
                if self._clip_wins_range(clip, global_start, global_end):
                    pen_color = "#174a8b" if clip.id == self._selected_clip_id else "#2457a6"
                else:
                    pen_color = "#8d8d8d"
                painter.setPen(QPen(QColor(pen_color), 1))
                painter.drawLine(x, y1, x, y2)

    def _draw_recorded_region(self, painter: QPainter, rect) -> None:
        if self._timeline_size() == 0:
            return
        if self._clips:
            for clip in sorted(self._clips, key=self._clip_priority):
                lane_rect = self._clip_lane_rect(rect, clip)
                self._draw_clip_region(painter, lane_rect, clip, self._clip_header_height(lane_rect))
            self._draw_overlap_summary(painter, rect)
            return

        audio_left = int(self._x_at_sample(0))
        audio_right = int(self._x_at_sample(self._samples.size))
        visible_left = max(rect.left(), audio_left)
        visible_right = min(rect.right(), audio_right)
        if visible_right <= visible_left:
            return

        painter.fillRect(visible_left, rect.top() + 1, visible_right - visible_left, rect.height() - 1, QColor("#edf3fb"))

        painter.setPen(QPen(QColor("#b8c8da"), 1))
        painter.drawLine(visible_left, rect.top() + 1, visible_right, rect.top() + 1)
        painter.drawLine(visible_left, rect.bottom(), visible_right, rect.bottom())

        if audio_left >= rect.left() and audio_left <= rect.right():
            painter.setPen(QPen(QColor("#8ca4bf"), 2))
            painter.drawLine(audio_left, rect.top() + 1, audio_left, rect.bottom())
        if audio_right >= rect.left() and audio_right <= rect.right():
            painter.setPen(QPen(QColor("#8ca4bf"), 2))
            painter.drawLine(audio_right, rect.top() + 1, audio_right, rect.bottom())

    def _draw_clip_region(self, painter: QPainter, lane_rect, clip: WaveformClip, header_height: int) -> None:
        clip_left = int(self._x_at_sample(clip.start_sample))
        clip_right = int(self._x_at_sample(clip.end_sample))
        visible_left = max(lane_rect.left(), clip_left)
        visible_right = min(lane_rect.right(), clip_right)
        if visible_right <= visible_left:
            return

        selected = clip.id == self._selected_clip_id
        audible_color = QColor("#e8f0fb" if selected else "#edf3fb")
        muted_color = QColor("#e8e8e8" if selected else "#f0f0f0")
        border_color = QColor("#2f6db1" if selected else "#9fb6cf")
        header_audible = QColor("#cfe1f4" if selected else "#dbe8f6")
        header_muted = QColor("#d7d7d7")

        for start, end, audible, overlapped in self._clip_audibility_segments(clip):
            segment_left = max(visible_left, int(self._x_at_sample(start)))
            segment_right = min(visible_right, int(self._x_at_sample(end)))
            if segment_right <= segment_left:
                continue
            painter.fillRect(
                segment_left,
                lane_rect.top() + 1,
                segment_right - segment_left,
                lane_rect.height() - 1,
                audible_color if audible else muted_color,
            )
            painter.fillRect(
                segment_left,
                lane_rect.top() + 1,
                segment_right - segment_left,
                min(header_height, lane_rect.height() - 1),
                header_audible if audible else header_muted,
            )
            if audible and overlapped:
                painter.fillRect(
                    segment_left,
                    lane_rect.top() + 1,
                    segment_right - segment_left,
                    4,
                    QColor("#d99a2b"),
                )

        painter.setPen(QPen(border_color, 1 if not selected else 2))
        painter.drawRect(clip_left, lane_rect.top() + 1, max(1, clip_right - clip_left), lane_rect.height() - 2)
        painter.setPen(QPen(QColor("#7999ba"), 1))
        painter.drawLine(visible_left, lane_rect.top() + header_height, visible_right, lane_rect.top() + header_height)

    def _draw_selection(self, painter: QPainter, rect) -> None:
        if not self.has_selection():
            return
        x1 = self._x_at_sample(self._selection_start)
        x2 = self._x_at_sample(self._selection_end)
        left = int(x1)
        width = max(1, int(x2 - x1))
        if left > rect.right() or left + width < rect.left():
            return
        visible_left = max(left, rect.left())
        visible_width = min(left + width, rect.right()) - visible_left
        if visible_width <= 0:
            return
        painter.fillRect(visible_left, rect.top(), visible_width, rect.height(), QColor(229, 238, 255))
        painter.setPen(QPen(QColor("#184e96"), 1))
        painter.drawRect(left, rect.top(), width, rect.height())
        painter.fillRect(left - 3, rect.top(), 6, rect.height(), QColor("#184e96"))
        painter.fillRect(left + width - 3, rect.top(), 6, rect.height(), QColor("#184e96"))

        selected_seconds = (self._selection_end - self._selection_start) / float(self._sample_rate)
        text = f"{selected_seconds:.3f}s"
        metrics = QFontMetrics(painter.font())
        text_x = left + max(6, (width - metrics.horizontalAdvance(text)) // 2)
        painter.setPen(QColor("#184e96"))
        painter.drawText(text_x, rect.top() + metrics.ascent() + 6, text)

    def _draw_duration(self, painter: QPainter, rect) -> None:
        painter.setPen(QColor("#595959"))
        text = format_duration(self._samples, self._sample_rate)
        painter.drawText(rect.left() + 8, rect.bottom() - 8, text)

    def _draw_playhead(self, painter: QPainter, rect) -> None:
        if self._playhead_sample is None or self._timeline_size() == 0:
            return
        x = int(self._x_at_sample(self._playhead_sample))
        if x < rect.left() or x > rect.right():
            return
        painter.setPen(QPen(QColor("#d0342c"), 2))
        painter.drawLine(x, rect.top(), x, rect.bottom())
        painter.setBrush(QColor("#d0342c"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(
            QPolygon(
                [
                    rect.topLeft() + QPoint(x - rect.left(), 0),
                    rect.topLeft() + QPoint(x - rect.left() - 6, -8),
                    rect.topLeft() + QPoint(x - rect.left() + 6, -8),
                ]
            )
        )

    def _draw_cursor(self, painter: QPainter, rect) -> None:
        if self._timeline_size() == 0:
            return
        x = int(self._x_at_sample(self._cursor_sample))
        if x < rect.left() or x > rect.right():
            return
        painter.setPen(QPen(QColor("#222222"), 1))
        painter.drawLine(x, rect.top(), x, rect.bottom())
        painter.setBrush(QColor("#222222"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(
            QPolygon(
                [
                    rect.topLeft() + QPoint(x - rect.left(), -1),
                    rect.topLeft() + QPoint(x - rect.left() - 5, -9),
                    rect.topLeft() + QPoint(x - rect.left() + 5, -9),
                ]
            )
        )

    def _draw_ruler(self, painter: QPainter, rect) -> None:
        ruler_top = self.rect().top() + 8
        ruler_bottom = rect.top() - 5
        painter.setPen(QPen(QColor("#cfcfcf"), 1))
        painter.drawLine(rect.left(), ruler_bottom, rect.right(), ruler_bottom)

        if self._timeline_size() == 0:
            return

        visible_duration = self._visible_samples() / float(self._sample_rate)
        major_step = self._major_tick_step(visible_duration)
        minor_step = major_step / 5.0
        metrics = QFontMetrics(painter.font())

        start_seconds = self._view_start_sample / float(self._sample_rate)
        end_seconds = (self._view_start_sample + self._visible_samples()) / float(self._sample_rate)
        tick = np.floor(start_seconds / minor_step) * minor_step
        while tick <= end_seconds + 0.0001:
            sample = int(round(tick * self._sample_rate))
            x = int(self._x_at_sample(sample))
            is_major = abs((tick / major_step) - round(tick / major_step)) < 0.0001
            tick_height = 14 if is_major else 7
            painter.setPen(QPen(QColor("#9f9f9f" if is_major else "#c7c7c7"), 1))
            painter.drawLine(x, ruler_bottom, x, ruler_bottom - tick_height)
            if is_major:
                label = self._format_tick_label(tick)
                painter.setPen(QColor("#555555"))
                painter.drawText(x + 4, ruler_top + metrics.ascent(), label)
            tick += minor_step

    def _sample_at_x(self, x: float) -> int:
        rect = self._content_rect()
        timeline_size = self._timeline_size()
        if timeline_size == 0 or rect.width() <= 0:
            return 0
        offset = max(0.0, float(x) - rect.left())
        sample = int(round(self._view_start_sample + offset * self._samples_per_pixel))
        return max(0, min(sample, timeline_size))

    def _x_at_sample(self, sample: int) -> float:
        rect = self._content_rect()
        return rect.left() + (sample - self._view_start_sample) / self._samples_per_pixel

    def _content_rect(self):
        return self.rect().adjusted(12, 34, -12, -12)

    @staticmethod
    def _major_tick_step(duration: float) -> float:
        if duration <= 5:
            return 1.0
        if duration <= 15:
            return 2.0
        if duration <= 45:
            return 5.0
        if duration <= 120:
            return 10.0
        return 30.0

    @staticmethod
    def _format_tick_label(seconds: float) -> str:
        rounded = int(round(seconds))
        minutes = rounded // 60
        remainder = rounded % 60
        if minutes:
            return f"{minutes}:{remainder:02d}"
        return f"{remainder}s"

    def _format_sample_time(self, sample: int) -> str:
        seconds = max(0.0, sample / float(self._sample_rate))
        minutes = int(seconds // 60)
        remainder = seconds - (minutes * 60)
        if minutes:
            return f"{minutes}:{remainder:05.2f}"
        return f"{remainder:.2f}s"

    def _hit_test(self, x: float) -> str:
        if not self.has_selection():
            return "select"

        start_x = self._x_at_sample(self._selection_start)
        end_x = self._x_at_sample(self._selection_end)
        handle_width = 8
        if abs(x - start_x) <= handle_width:
            return "start"
        if abs(x - end_x) <= handle_width:
            return "end"
        if start_x < x < end_x:
            return "move"
        return "select"

    def _hit_clip_header(self, x: float, y: float) -> int | None:
        rect = self._content_rect()
        sample = self._sample_at_x(x)
        for clip in sorted(self._clips, key=self._clip_priority, reverse=True):
            lane_rect = self._clip_lane_rect(rect, clip)
            header_bottom = lane_rect.top() + self._clip_header_height(lane_rect)
            if (
                lane_rect.top() <= y <= header_bottom
                and clip.start_sample <= sample < clip.end_sample
            ):
                return clip.id
        return None

    def _clips_at_position(self, x: float, y: float) -> list[WaveformClip]:
        rect = self._content_rect()
        if x < rect.left() or x > rect.right() or y < rect.top() or y > rect.bottom():
            return []
        return self._clips_at_sample(self._sample_at_x(x))

    def _clips_at_sample(self, sample: int) -> list[WaveformClip]:
        return sorted(
            [
                clip
                for clip in self._clips
                if clip.samples.size > 0 and clip.start_sample <= sample < clip.end_sample
            ],
            key=self._clip_priority,
            reverse=True,
        )

    def _clip_menu_label(self, clip: WaveformClip, clips_under_mouse: list[WaveformClip]) -> str:
        status = "front" if clips_under_mouse and clips_under_mouse[0].id == clip.id else "behind"
        return (
            f"Clip {clip.id} "
            f"{self._format_sample_time(clip.start_sample)}-{self._format_sample_time(clip.end_sample)} "
            f"({status})"
        )

    def _clip_is_front(self, clip_id: int) -> bool:
        clip = self._clip_by_id(clip_id)
        if clip is None or not self._clips:
            return False
        return clip.id == max(self._clips, key=self._clip_priority).id

    def _clip_is_back(self, clip_id: int) -> bool:
        clip = self._clip_by_id(clip_id)
        if clip is None or not self._clips:
            return False
        return clip.id == min(self._clips, key=self._clip_priority).id

    def _clip_by_id(self, clip_id: int | None) -> WaveformClip | None:
        if clip_id is None:
            return None
        return next((clip for clip in self._clips if clip.id == clip_id), None)

    def _assign_clip_lanes(self) -> None:
        self._clip_lanes = {clip.id: 0 for clip in self._clips}
        self._lane_count = 1

    def _clip_lane_rect(self, rect, clip: WaveformClip) -> QRect:
        lane = max(0, self._clip_lanes.get(clip.id, 0))
        top = rect.top() + round(lane * rect.height() / self._lane_count)
        bottom = rect.top() + round((lane + 1) * rect.height() / self._lane_count) - 1
        return QRect(rect.left(), top, rect.width(), max(1, bottom - top + 1))

    def _clip_priority(self, clip: WaveformClip) -> tuple[int, int]:
        return clip.layer, clip.id

    def _winning_clip_at_sample(self, sample: int) -> WaveformClip | None:
        candidates = [
            clip
            for clip in self._clips
            if clip.samples.size > 0 and clip.start_sample <= sample < clip.end_sample
        ]
        if not candidates:
            return None
        return max(candidates, key=self._clip_priority)

    def _overlap_count_at_sample(self, sample: int) -> int:
        return sum(1 for clip in self._clips if clip.start_sample <= sample < clip.end_sample)

    def _clip_wins_range(self, clip: WaveformClip, start: int, end: int) -> bool:
        if end <= start:
            sample = start
        else:
            sample = start + (end - start) // 2
        sample = max(clip.start_sample, min(sample, clip.end_sample - 1))
        winner = self._winning_clip_at_sample(sample)
        return winner is not None and winner.id == clip.id

    def _clip_audibility_segments(self, clip: WaveformClip) -> list[tuple[int, int, bool, bool]]:
        breakpoints = {clip.start_sample, clip.end_sample}
        for other in self._clips:
            if other.id == clip.id or other.end_sample <= clip.start_sample or other.start_sample >= clip.end_sample:
                continue
            breakpoints.add(max(clip.start_sample, other.start_sample))
            breakpoints.add(min(clip.end_sample, other.end_sample))

        ordered = sorted(breakpoints)
        segments: list[tuple[int, int, bool, bool]] = []
        for start, end in zip(ordered, ordered[1:], strict=False):
            if end <= start:
                continue
            sample = start + (end - start) // 2
            winner = self._winning_clip_at_sample(sample)
            audible = winner is not None and winner.id == clip.id
            overlapped = self._overlap_count_at_sample(sample) > 1
            segments.append((start, end, audible, overlapped))
        return segments

    def _draw_lane_guides(self, painter: QPainter, rect) -> None:
        if self._lane_count <= 1:
            return
        painter.setPen(QPen(QColor("#e2e2e2"), 1))
        for lane in range(1, self._lane_count):
            y = rect.top() + round(lane * rect.height() / self._lane_count)
            painter.drawLine(rect.left(), y, rect.right(), y)

    def _draw_overlap_summary(self, painter: QPainter, rect) -> None:
        for start, end in self._overlap_segments():
            left = max(rect.left(), int(self._x_at_sample(start)))
            right = min(rect.right(), int(self._x_at_sample(end)))
            if right <= left:
                continue
            painter.fillRect(left, rect.top() + 1, right - left, 4, QColor("#d99a2b"))
            painter.fillRect(left, rect.bottom() - 4, right - left, 3, QColor("#a4a4a4"))

    def _overlap_segments(self) -> list[tuple[int, int]]:
        breakpoints = sorted(
            {
                sample
                for clip in self._clips
                for sample in (clip.start_sample, clip.end_sample)
                if clip.samples.size > 0
            }
        )
        segments: list[tuple[int, int]] = []
        for start, end in zip(breakpoints, breakpoints[1:], strict=False):
            if end <= start:
                continue
            sample = start + (end - start) // 2
            if self._overlap_count_at_sample(sample) > 1:
                segments.append((start, end))
        return segments

    @staticmethod
    def _clip_header_height(rect) -> int:
        return max(16, min(24, rect.height() // 5))

    def _update_cursor(self, x: float, y: float | None = None) -> None:
        if y is not None and self._hit_clip_header(x, y) is not None:
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            return
        hit = self._hit_test(x)
        if hit in ("start", "end"):
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif hit == "move":
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.setCursor(Qt.CursorShape.IBeamCursor)

    def wheelEvent(self, event) -> None:
        if self._timeline_size() == 0:
            return
        delta = event.angleDelta().x() or event.angleDelta().y()
        if delta == 0:
            return
        step = max(1, self._visible_samples() // 8)
        direction = -1 if delta > 0 else 1
        self.set_view_start(self._view_start_sample + direction * step)
        event.accept()

    def _visible_samples(self) -> int:
        rect = self._content_rect()
        return max(1, int(round(rect.width() * self._samples_per_pixel)))

    def _clamp_view_start(self, sample: int) -> int:
        max_start = max(0, self._timeline_size() - self._visible_samples())
        return max(0, min(int(sample), max_start))

    def _emit_viewport_changed(self) -> None:
        value, maximum, page_step = self.scrollbar_state()
        self.viewportChanged.emit(value, maximum, page_step)

    def _default_samples_per_pixel(self) -> float:
        return max(1.0, self._sample_rate / self.DEFAULT_PIXELS_PER_SECOND)

    def _timeline_size(self) -> int:
        return max(self._samples.size, max((clip.end_sample for clip in self._clips), default=0))
