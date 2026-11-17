"""Centre viewport: plays the original video, and shows processed preview frames when paused.

Real-time playback with effects is not possible with Qt's player, so playback is honestly labelled
"Original"; the moment playback pauses (or the user scrubs), the edited frame at that position is
shown, rendered by the preview engine with the export's own filters.
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl, Signal, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSlider, QStackedLayout, QVBoxLayout, QWidget

from ..media.probe import format_duration
from . import theme
from .icons import icon
from .viewer import FrameView
from .widgets import tool_button


def _fmt_ms(ms: int) -> str:
    return format_duration(ms / 1000)


class Player(QWidget):
    position_settled = Signal(float)   # seconds, when paused/scrubbed (request an edited frame here)
    playing_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._fps = 30.0
        self._scrubbing = False
        self._range_end: int | None = None
        self._still = False
        self.player = QMediaPlayer(self)
        # FRAMEWISE_NO_AUDIO=1 skips audio output: needed on headless machines (tests, CI) without a sound device
        self.audio = None if os.environ.get("FRAMEWISE_NO_AUDIO") == "1" else QAudioOutput(self)
        if self.audio is not None:
            self.player.setAudioOutput(self.audio)
        self.video = QVideoWidget()
        self.video.setMinimumSize(320, 180)
        self.video.setStyleSheet(f"background: {theme.VIEWER_BG};")
        self.player.setVideoOutput(self.video)

        self.message = QLabel("Select media to preview it")
        self.message.setAlignment(Qt.AlignCenter)
        self.message.setObjectName("muted")
        self.message.setStyleSheet(f"background: {theme.VIEWER_BG};")
        self.frame_view = FrameView()
        self.stack = QStackedLayout()
        for w in (self.message, self.video, self.frame_view):
            self.stack.addWidget(w)
        screen = QWidget()
        screen.setLayout(self.stack)
        self.badge = QLabel("Original playback")
        self.badge.setObjectName("badge")
        self.badge.setVisible(False)

        self.btn_back = tool_button("chevron-left", "Previous frame (←)", lambda: self.step(-1))
        self.btn_play = tool_button("play", "Play / pause (Space)", self.toggle, size=18)
        self.btn_fwd = tool_button("chevron-right", "Next frame (→)", lambda: self.step(1))
        self.btn_mute = tool_button("volume-2", "Mute", self._toggle_mute)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderPressed.connect(self._scrub_start)
        self.slider.sliderReleased.connect(self._scrub_end)
        self.slider.sliderMoved.connect(self.player.setPosition)
        self.time = QLabel("0:00 / 0:00")
        self.time.setMinimumWidth(110)
        self.time.setObjectName("muted")

        self.controls = QWidget()
        controls = QHBoxLayout(self.controls)
        controls.setContentsMargins(4, 2, 4, 2)
        for w in (self.btn_back, self.btn_play, self.btn_fwd):
            controls.addWidget(w)
        controls.addWidget(self.slider, 1)
        controls.addWidget(self.time)
        controls.addWidget(self.badge)
        controls.addWidget(self.btn_mute)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(screen, 1)
        lay.addWidget(self.controls)

        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.errorOccurred.connect(self._on_error)
        self.setFocusPolicy(Qt.StrongFocus)
        self._set_enabled(False)

    # -- public API ---------------------------------------------------------------------------
    def set_fps(self, fps: float | None) -> None:
        self._fps = fps or 30.0

    def position_seconds(self) -> float:
        return self.player.position() / 1000

    def load(self, path: Path | None, fps: float | None = None, still: bool = False, has_video: bool = True) -> None:
        """Load media. Still images show only the frame view; audio files keep the transport."""
        self.player.stop()
        self._fps = fps or 30.0
        self._still = still
        self._range_end = None
        self.frame_view.clear()
        if path is None:
            self.player.setSource(QUrl())
            self._show_message("Select media to preview it")
            self._set_enabled(False)
            return
        if not Path(path).is_file():
            self.player.setSource(QUrl())
            self._show_message(f"File not found:\n{path}")
            self._set_enabled(False)
            return
        if still:
            self.player.setSource(QUrl())
            self.stack.setCurrentWidget(self.frame_view)
            self.controls.setVisible(False)
            return
        self.controls.setVisible(True)
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self._set_enabled(True)
        if has_video:
            self.stack.setCurrentWidget(self.frame_view)
        else:
            self._show_message("Audio file: press play to listen")
        self.player.pause()

    def toggle(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self._range_end = None
            self.player.play()

    def play_range(self, start: float, end: float) -> None:
        """Play only [start, end] (trim preview), then pause on the end frame."""
        self.player.setPosition(int(start * 1000))
        self._range_end = int(end * 1000)
        self.player.play()

    def step(self, frames: int) -> None:
        self.player.pause()
        ms = int(round(1000 * frames / self._fps))
        self.player.setPosition(max(0, min(self.player.duration(), self.player.position() + ms)))
        self.position_settled.emit(self.position_seconds())

    def seek(self, seconds: float) -> None:
        self.player.setPosition(int(seconds * 1000))
        self.position_settled.emit(seconds)

    def stop(self) -> None:
        self.player.stop()

    # -- internals ------------------------------------------------------------------------------
    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key_Space:
            self.toggle()
        elif e.key() == Qt.Key_Left:
            self.step(-1)
        elif e.key() == Qt.Key_Right:
            self.step(1)
        else:
            super().keyPressEvent(e)

    def _set_enabled(self, on: bool) -> None:
        for w in (self.btn_play, self.btn_back, self.btn_fwd, self.slider):
            w.setEnabled(on)
        self.btn_mute.setEnabled(on and self.audio is not None)
        if not on:
            self.slider.setRange(0, 0)
            self.time.setText("0:00 / 0:00")

    def _show_message(self, text: str) -> None:
        self.message.setText(text)
        self.stack.setCurrentWidget(self.message)

    def _toggle_mute(self) -> None:
        if self.audio is None:
            return
        self.audio.setMuted(not self.audio.isMuted())
        self.btn_mute.setIcon(icon("volume-x" if self.audio.isMuted() else "volume-2", theme.TEXT))

    def _scrub_start(self) -> None:
        self._scrubbing = True

    def _scrub_end(self) -> None:
        self._scrubbing = False
        self.player.setPosition(self.slider.value())
        self.position_settled.emit(self.slider.value() / 1000)

    @Slot(int)
    def _on_position(self, ms: int) -> None:
        if not self._scrubbing:
            self.slider.setValue(ms)
        self.time.setText(f"{_fmt_ms(ms)} / {_fmt_ms(self.player.duration())}")
        if self._range_end is not None and ms >= self._range_end:
            self._range_end = None
            self.player.pause()

    @Slot(int)
    def _on_duration(self, ms: int) -> None:
        self.slider.setRange(0, ms)
        self.time.setText(f"{_fmt_ms(self.player.position())} / {_fmt_ms(ms)}")

    def _on_state(self, state) -> None:
        playing = state == QMediaPlayer.PlayingState
        self.btn_play.setIcon(icon("pause" if playing else "play", theme.TEXT))
        self.badge.setVisible(playing)
        if playing and self.stack.currentWidget() is self.frame_view:
            self.stack.setCurrentWidget(self.video)
        elif not playing and self.stack.currentWidget() is self.video:
            self.stack.setCurrentWidget(self.frame_view)
            self.position_settled.emit(self.position_seconds())
        self.playing_changed.emit(playing)

    def _on_error(self, _err, text: str) -> None:
        if text:
            self.frame_view.message = f"The built-in player can't play this file ({text}).\nIt can still be edited and exported."
            self.frame_view.update()
