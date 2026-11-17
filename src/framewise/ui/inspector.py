"""Settings for the selected media item. Emits edits; never mutates the project itself."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QSlider, QVBoxLayout, QWidget)

from ..media.probe import format_duration, format_size
from ..project.model import QUALITY_RANGE, MediaItem
from ..render.presets import PRESETS, get_preset
from ..render.scaling import TARGET_CHOICES, plan_scaling
from ..tools.encoders import ENCODERS


def quality_note(q: int) -> str:
    if q <= 16:
        return "near-lossless, large file"
    if q <= 21:
        return "high quality"
    if q <= 26:
        return "balanced"
    return "small file"


class Inspector(QWidget):
    changed = Signal(str, dict)          # item id, {field: value}
    preview_requested = Signal(str)
    queue_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._item: MediaItem | None = None
        self._loading = False
        self._encoders: list[str] = list(ENCODERS)

        self.title = QLabel("No video selected")
        self.title.setObjectName("heading")
        self.title.setWordWrap(True)
        self.facts = QLabel("")
        self.facts.setObjectName("muted")
        self.facts.setWordWrap(True)

        # AI upscale group
        self.chk_upscale = QCheckBox("AI upscale")
        self.cmb_preset = QComboBox()
        for p in PRESETS.values():
            self.cmb_preset.addItem(p.label, p.key)
        self.lbl_preset = QLabel()
        self.lbl_preset.setObjectName("muted")
        self.lbl_preset.setWordWrap(True)
        self.cmb_target = QComboBox()
        self.cmb_target.addItems(TARGET_CHOICES)
        self.lbl_size = QLabel()
        up_form = QFormLayout()
        up_form.addRow(self.chk_upscale)
        up_form.addRow("Content", self.cmb_preset)
        up_form.addRow("", self.lbl_preset)
        up_form.addRow("Upscale to", self.cmb_target)
        up_form.addRow("Output size", self.lbl_size)
        self.grp_upscale = QGroupBox("Enhance")
        self.grp_upscale.setLayout(up_form)

        # Export group
        self.cmb_encoder = QComboBox()
        self.sld_quality = QSlider(Qt.Horizontal)
        self.sld_quality.setRange(*QUALITY_RANGE)
        self.sld_quality.setInvertedAppearance(True)  # right = better (lower CRF)
        self.lbl_quality = QLabel()
        self.lbl_quality.setMinimumWidth(140)
        qrow = QHBoxLayout()
        qrow.addWidget(self.sld_quality, 1)
        qrow.addWidget(self.lbl_quality)
        self.txt_output = QLineEdit()
        self.txt_output.setPlaceholderText("Automatic (next to the source)")
        btn_browse = QPushButton("Browse")
        btn_browse.clicked.connect(self._browse_output)
        orow = QHBoxLayout()
        orow.addWidget(self.txt_output, 1)
        orow.addWidget(btn_browse)
        ex_form = QFormLayout()
        ex_form.addRow("Encoder", self.cmb_encoder)
        ex_form.addRow("Quality", qrow)
        ex_form.addRow("Save as", orow)
        self.grp_export = QGroupBox("Export")
        self.grp_export.setLayout(ex_form)

        self.warning = QLabel()
        self.warning.setObjectName("warning")
        self.warning.setWordWrap(True)
        self.btn_preview = QPushButton("Preview 3 sec")
        self.btn_preview.setToolTip("Render the first 3 seconds with these settings")
        self.btn_queue = QPushButton("Add to render queue")
        self.btn_queue.setObjectName("primary")
        self.btn_preview.clicked.connect(lambda: self._item and self.preview_requested.emit(self._item.id))
        self.btn_queue.clicked.connect(lambda: self._item and self.queue_requested.emit(self._item.id))
        brow = QHBoxLayout()
        brow.addWidget(self.btn_preview)
        brow.addWidget(self.btn_queue, 1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(self.title)
        lay.addWidget(self.facts)
        lay.addWidget(self.grp_upscale)
        lay.addWidget(self.grp_export)
        lay.addWidget(self.warning)
        lay.addStretch()
        lay.addLayout(brow)

        self.chk_upscale.toggled.connect(lambda v: self._emit(upscale=v))
        self.cmb_preset.currentIndexChanged.connect(lambda _i: self._emit(preset=self.cmb_preset.currentData()))
        self.cmb_target.currentTextChanged.connect(lambda t: self._emit(target=t))
        self.cmb_encoder.currentIndexChanged.connect(lambda _i: self._emit(encoder=self.cmb_encoder.currentData()))
        self.sld_quality.valueChanged.connect(self._quality_moved)
        self.sld_quality.sliderReleased.connect(lambda: self._emit(quality=self.sld_quality.value()))
        self.txt_output.editingFinished.connect(lambda: self._emit(output=self.txt_output.text().strip()))
        self.set_item(None)

    # -- public ---------------------------------------------------------------
    def set_encoders(self, keys: list[str]) -> None:
        self._encoders = keys or ["libx264"]
        self.set_item(self._item)

    def set_item(self, item: MediaItem | None) -> None:
        self._item = item
        self._loading = True
        try:
            enabled = item is not None
            for w in (self.grp_upscale, self.grp_export, self.btn_preview, self.btn_queue):
                w.setEnabled(enabled)
            if item is None:
                self.title.setText("No video selected")
                self.facts.setText("Select one video in the media list to edit its settings.")
                self.warning.clear()
                return
            s = item.settings
            self.title.setText(item.name)
            if item.info:
                i = item.info
                audio = i.audio_codec or "no audio"
                self.facts.setText(f"{i.resolution} · {i.fps:.3g} fps · {format_duration(i.duration)}\n"
                                   f"{i.video_codec} / {audio} · {format_size(i.size_bytes)}")
            else:
                self.facts.setText("Reading file information…" if item.exists() else "File missing")
            self.chk_upscale.setChecked(s.upscale)
            self.cmb_preset.setCurrentIndex(max(0, self.cmb_preset.findData(s.preset)))
            self.cmb_target.setCurrentText(s.target)
            self.cmb_encoder.clear()
            keys = list(self._encoders) + ([s.encoder] if s.encoder not in self._encoders else [])
            for k in keys:
                label = ENCODERS[k].label + ("" if k in self._encoders else " (not available)")
                self.cmb_encoder.addItem(label, k)
            self.cmb_encoder.setCurrentIndex(max(0, self.cmb_encoder.findData(s.encoder)))
            self.sld_quality.setValue(s.quality)
            self.txt_output.setText(s.output)
            for w in (self.cmb_preset, self.cmb_target):
                w.setEnabled(s.upscale)
            self._refresh_derived()
        finally:
            self._loading = False

    # -- internals -------------------------------------------------------------
    def _emit(self, **changes) -> None:
        if not self._loading and self._item is not None:
            self.changed.emit(self._item.id, changes)

    def _quality_moved(self, v: int) -> None:
        self.lbl_quality.setText(f"{v} · {quality_note(v)}")
        if not self.sld_quality.isSliderDown():  # keyboard / click: commit immediately
            self._emit(quality=v)

    def _refresh_derived(self) -> None:
        item = self._item
        if item is None:
            return
        s = item.settings
        self.lbl_preset.setText(get_preset(s.preset).description)
        self.lbl_quality.setText(f"{s.quality} · {quality_note(s.quality)}")
        problems = []
        if not item.exists():
            problems.append("The source file is missing. Re-import it.")
        if s.upscale and item.info:
            try:
                plan = plan_scaling(item.info.width, item.info.height, s.target, get_preset(s.preset).native_scales)
                self.lbl_size.setText(f"{plan.out_width}×{plan.out_height}")
            except ValueError as e:
                self.lbl_size.setText("—")
                problems.append(str(e))
        else:
            self.lbl_size.setText(item.info.resolution if item.info else "—")
        if s.encoder not in self._encoders:
            problems.append("The chosen encoder isn't available on this computer.")
        self.warning.setText("\n".join(problems))
        self.btn_queue.setEnabled(not problems)
        self.btn_preview.setEnabled(not problems)

    def _browse_output(self) -> None:
        if self._item is None:
            return
        start = self.txt_output.text() or str(Path(self._item.path).with_name(Path(self._item.path).stem + "_out.mp4"))
        path, _ = QFileDialog.getSaveFileName(self, "Save rendered video as", start, "MP4 video (*.mp4)")
        if path:
            if not path.lower().endswith(".mp4"):
                path += ".mp4"
            self.txt_output.setText(path)
            self._emit(output=path)
