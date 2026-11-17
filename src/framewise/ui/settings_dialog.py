"""Settings: default output folder, encoder, quality and preview behaviour."""
from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
                               QLineEdit, QPushButton, QSpinBox, QVBoxLayout)

from ..core.settings import Settings
from ..tools.encoders import ENCODERS
from .widgets import muted

PREVIEW_SIZES = [("960 × 540 (fast)", 960), ("1280 × 720", 1280), ("1920 × 1080 (sharp)", 1920)]


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, encoders: list[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("FrameWise settings")
        self.setMinimumWidth(460)
        self.settings = settings
        self.out_dir = QLineEdit(settings.output_dir)
        self.out_dir.setPlaceholderText("Next to each source file")
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.out_dir, 1)
        row.addWidget(browse)
        self.encoder = QComboBox()
        for k in encoders or ["libx264"]:
            self.encoder.addItem(ENCODERS[k].label, k)
        self.encoder.setCurrentIndex(max(0, self.encoder.findData(settings.default_encoder)))
        self.quality = QSpinBox()
        self.quality.setRange(10, 35)
        self.quality.setValue(settings.default_quality)
        self.preview = QComboBox()
        for label, w in PREVIEW_SIZES:
            self.preview.addItem(label, w)
        self.preview.setCurrentIndex(max(0, self.preview.findData(settings.preview_width)))
        self.ai_preview = QCheckBox("Include AI upscaling in preview frames (slower)")
        self.ai_preview.setChecked(settings.preview_ai)
        form = QFormLayout()
        form.addRow("Export folder", row)
        form.addRow("Default codec", self.encoder)
        form.addRow("Default quality", self.quality)
        form.addRow("Preview size", self.preview)
        form.addRow("", self.ai_preview)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(muted("Quality uses the CRF scale: lower is better and larger (18 is visually lossless)."))
        lay.addWidget(buttons)

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Export folder", self.out_dir.text())
        if d:
            self.out_dir.setText(d)

    def _save(self) -> None:
        s = self.settings
        s.output_dir = self.out_dir.text().strip()
        s.default_encoder = self.encoder.currentData()
        s.default_quality = self.quality.value()
        s.preview_width = self.preview.currentData()
        s.preview_ai = self.ai_preview.isChecked()
        self.accept()
