"""Video preview player with seeking and frame stepping."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QSlider, QStackedLayout, QStyle, QToolButton,
                               QVBoxLayout, QWidget)

from ..media.probe import format_duration


def _fmt_ms(ms: int) -> str:
    return format_duration(ms / 1000)


class Player(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._fps = 30.0
        self._scrubbing = False
        self.player = QMediaPlayer(self)
        # FRAMEWISE_NO_AUDIO=1 skips audio output: needed on headless machines (tests, CI) without a sound device
        self.audio = None if os.environ.get("FRAMEWISE_NO_AUDIO") == "1" else QAudioOutput(self)
        if self.audio is not None:
            self.player.setAudioOutput(self.audio)
        self.video = QVideoWidget()
        self.video.setMinimumSize(320, 180)
        self.video.setStyleSheet("background: black;")
        self.player.setVideoOutput(self.video)

        self.message = QLabel("Select a video to preview it")
        self.message.setAlignment(Qt.AlignCenter)
        self.message.setObjectName("muted")
        self.message.setStyleSheet("background: #111;")
        self.stack = QStackedLayout()
        self.stack.addWidget(self.message)
        self.stack.addWidget(self.video)
        screen = QWidget()
        screen.setLayout(self.stack)

        st = self.style()
        self.btn_play = self._button(st.standardIcon(QStyle.SP_MediaPlay), "Play / pause (Space)", self.toggle)
        self.btn_back = self._button(st.standardIcon(QStyle.SP_MediaSeekBackward), "Previous frame (←)",
                                     lambda: self.step(-1))
        self.btn_fwd = self._button(st.standardIcon(QStyle.SP_MediaSeekForward), "Next frame (→)",
                                    lambda: self.step(1))
        self.btn_mute = self._button(st.standardIcon(QStyle.SP_MediaVolume), "Mute", self._toggle_mute)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderPressed.connect(self._scrub_start)
        self.slider.sliderReleased.connect(self._scrub_end)
        self.slider.sliderMoved.connect(self.player.setPosition)
        self.time = QLabel("0:00 / 0:00")
        self.time.setMinimumWidth(110)

        controls = QHBoxLayout()
        for w in (self.btn_back, self.btn_play, self.btn_fwd):
            controls.addWidget(w)
        controls.addWidget(self.slider, 1)
        controls.addWidget(self.time)
        controls.addWidget(self.btn_mute)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(screen, 1)
        lay.addLayout(controls)

        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.errorOccurred.connect(self._on_error)
        self.setFocusPolicy(Qt.StrongFocus)
        self._set_enabled(False)

    def _button(self, icon, tip, slot) -> QToolButton:
        b = QToolButton()
        b.setIcon(icon)
        b.setToolTip(tip)
        b.clicked.connect(slot)
        return b

    # -- public API ---------------------------------------------------------
    def load(self, path: Path | None, fps: float | None = None) -> None:
        self.player.stop()
        self._fps = fps or 30.0
        if path is None:
            self.player.setSource(QUrl())
            self._show_message("Select a video to preview it")
            self._set_enabled(False)
            return
        if not Path(path).is_file():
            self.player.setSource(QUrl())
            self._show_message(f"File not found:\n{path}")
            self._set_enabled(False)
            return
        self.stack.setCurrentWidget(self.video)
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self._set_enabled(True)
        self.player.pause()  # shows the first frame

    def set_fps(self, fps: float | None) -> None:
        self._fps = fps or 30.0

    def toggle(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def step(self, frames: int) -> None:
        self.player.pause()
        ms = int(round(1000 * frames / self._fps))
        self.player.setPosition(max(0, min(self.player.duration(), self.player.position() + ms)))

    def stop(self) -> None:
        self.player.stop()

    # -- internals ------------------------------------------------------------
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
        icon = QStyle.SP_MediaVolumeMuted if self.audio.isMuted() else QStyle.SP_MediaVolume
        self.btn_mute.setIcon(self.style().standardIcon(icon))

    def _scrub_start(self) -> None:
        self._scrubbing = True

    def _scrub_end(self) -> None:
        self._scrubbing = False
        self.player.setPosition(self.slider.value())

    @Slot(int)
    def _on_position(self, ms: int) -> None:
        if not self._scrubbing:
            self.slider.setValue(ms)
        self.time.setText(f"{_fmt_ms(ms)} / {_fmt_ms(self.player.duration())}")

    @Slot(int)
    def _on_duration(self, ms: int) -> None:
        self.slider.setRange(0, ms)
        self.time.setText(f"{_fmt_ms(self.player.position())} / {_fmt_ms(ms)}")

    def _on_state(self, state) -> None:
        icon = QStyle.SP_MediaPause if state == QMediaPlayer.PlayingState else QStyle.SP_MediaPlay
        self.btn_play.setIcon(self.style().standardIcon(icon))

    def _on_error(self, _err, text: str) -> None:
        if text:
            self._show_message(f"Can't preview this file in the player:\n{text}\n\nIt can still be rendered.")
