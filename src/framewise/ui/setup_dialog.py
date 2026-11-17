"""Download / check the external tools FrameWise needs."""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout)

from ..core.errors import Cancelled, FrameWiseError
from ..tools import installer, registry


class _Signals(QObject):
    progress = Signal(str, float)
    finished = Signal(str)  # "" on success, else error text ("cancelled" if cancelled)


class SetupDialog(QDialog):
    def __init__(self, tools_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("FrameWise tools")
        self.setMinimumWidth(480)
        self.tools_dir = tools_dir
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self.sig = _Signals(self)
        self.sig.progress.connect(self._on_progress)
        self.sig.finished.connect(self._on_finished)

        intro = QLabel("FrameWise uses two free tools that run on your computer. "
                       "They are downloaded once (about 100 MB) and stored in FrameWise's data folder.")
        intro.setWordWrap(True)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.hide()
        self.btn_download = QPushButton("Download")
        self.btn_download.setObjectName("primary")
        self.btn_close = QPushButton("Close")
        self.btn_download.clicked.connect(self._start)
        self.btn_close.clicked.connect(self._close_or_cancel)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self.btn_close)
        row.addWidget(self.btn_download)
        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        lay.addWidget(self.status)
        lay.addWidget(self.bar)
        lay.addLayout(row)
        self._refresh()

    def _missing(self) -> list[str]:
        return registry.missing_components(self.tools_dir)

    def _refresh(self) -> None:
        missing = self._missing()
        lines = []
        for comp, label in installer.COMPONENT_LABELS.items():
            if comp not in missing:
                lines.append(f"✔ {label}: installed")
            elif installer.can_auto_install(comp):
                lines.append(f"✖ {label}: will be downloaded")
            else:
                lines.append(f"✖ {label}: install it with your system's package manager")
        self.status.setText("\n".join(lines))
        downloadable = [c for c in missing if installer.can_auto_install(c)]
        self.btn_download.setEnabled(bool(downloadable))
        self.btn_download.setText("Download" if downloadable else "All set")

    def _start(self) -> None:
        todo = [c for c in self._missing() if installer.can_auto_install(c)]
        if not todo:
            return
        self._cancel.clear()
        self.btn_download.setEnabled(False)
        self.btn_close.setText("Cancel")
        self.bar.show()

        def work() -> None:
            try:
                for comp in todo:
                    installer.install(comp, self.tools_dir, lambda m, f: self.sig.progress.emit(m, f), self._cancel)
                self.sig.finished.emit("")
            except Cancelled:
                self.sig.finished.emit("cancelled")
            except (FrameWiseError, OSError) as e:
                self.sig.finished.emit(str(e))

        self._thread = threading.Thread(target=work, name="tool-download", daemon=True)
        self._thread.start()

    def _on_progress(self, msg: str, frac: float) -> None:
        self.bar.setValue(int(frac * 1000))
        self.bar.setFormat(f"{msg}  {frac:.0%}")

    def _on_finished(self, err: str) -> None:
        self._thread = None
        self.bar.hide()
        self.btn_close.setText("Close")
        self._refresh()
        if err == "cancelled":
            self.status.setText(self.status.text() + "\n\nDownload cancelled.")
        elif err:
            self.status.setText(self.status.text() + f"\n\nDownload failed:\n{err}")

    def _close_or_cancel(self) -> None:
        if self._thread is not None:
            self._cancel.set()
        else:
            self.accept()

    def reject(self) -> None:  # Esc / window close while downloading cancels first
        if self._thread is not None:
            self._cancel.set()
            return
        super().reject()
