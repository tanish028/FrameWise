"""Inspector: every edit for the selected item, in collapsible Midnight Studio sections.

The inspector never changes the project itself. It emits `changed(item_id, section, {field: value})`
once per user gesture (one undo step), and `previewed(...)` while a slider is being dragged so the
viewer can show the result live without filling the undo history.
"""
from __future__ import annotations

from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QScrollArea, QSlider, QSpinBox, QVBoxLayout, QWidget)

from ..media.probe import AUDIO, IMAGE, format_duration, format_size
from ..project.edit import (COLOR_ADVANCED, COLOR_BASIC, CROP_RATIOS, QUALITY_RANGE, SPEED_PRESETS, Edit)
from ..project.model import MediaItem
from ..render.export_presets import PRESETS as EXPORT_PRESETS
from ..render.graph import build_plan, exact_rate
from ..render.ops import LOOKS
from ..render.presets import PRESETS, get_preset
from ..render.scaling import TARGET_CHOICES
from ..tools.capabilities import Capabilities
from ..tools.encoders import ENCODERS, FORMAT_ENCODERS
from . import theme
from .icons import icon
from .widgets import Section, SliderRow, TimeEdit, muted, tool_button

RESOLUTIONS = [("Source size", 0, 0), ("3840 × 2160 (4K)", 3840, 2160), ("2560 × 1440", 2560, 1440),
               ("1920 × 1080", 1920, 1080), ("1280 × 720", 1280, 720), ("854 × 480", 854, 480),
               ("1080 × 1920 (vertical)", 1080, 1920), ("720 × 1280 (vertical)", 720, 1280),
               ("1080 × 1080 (square)", 1080, 1080), ("Custom…", -1, -1)]
FPS_CHOICES = [("Same as source", 0), ("24 fps", 24), ("25 fps", 25), ("30 fps", 30), ("50 fps", 50), ("60 fps", 60)]
VIDEO_FORMATS = [("MP4", "mp4"), ("MOV", "mov"), ("WebM", "webm"), ("GIF", "gif")]
IMAGE_FORMATS = [("PNG", "png"), ("JPEG", "jpeg"), ("WebP", "webp"), ("BMP", "bmp")]
AUDIO_FORMATS = [("MP3", "mp3"), ("M4A (AAC)", "m4a"), ("WAV", "wav"), ("FLAC", "flac")]
ALIGN = [("Fit (bars)", "fit"), ("Fill (crop)", "fill"), ("Blur fill", "blur"), ("Stretch", "stretch")]


def quality_note(q: int) -> str:
    if q <= 16:
        return "near-lossless, large file"
    if q <= 21:
        return "high quality"
    if q <= 26:
        return "balanced"
    return "small file"


def _combo(items: list[tuple[str, object]]) -> QComboBox:
    c = QComboBox()
    for label, data in items:
        c.addItem(label, data)
    return c


def _set_combo(c: QComboBox, data) -> None:
    i = c.findData(data)
    c.setCurrentIndex(max(0, i))


class Inspector(QWidget):
    changed = Signal(str, str, dict)          # item id, edit section, {field: value}
    previewed = Signal(str, str, dict)        # live (uncommitted) change while dragging
    preview_requested = Signal(str)           # render a short preview file
    queue_requested = Signal(str)             # export
    audio_export_requested = Signal(str)      # extract audio from a video
    crop_tool_toggled = Signal(bool)
    detect_bars_requested = Signal(str)
    trim_preview_requested = Signal(str)
    suggestion_accepted = Signal(str)         # apply the detected black-bar crop
    suggestion_dismissed = Signal(str)
    playhead_requested = Signal(str, str)     # item id, "start" | "end"
    edit_replaced = Signal(str, object, str)  # item id, new Edit, undo text (multi-section changes)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._item: MediaItem | None = None
        self._loading = False
        self._encoders: list[str] = [k for k, e in ENCODERS.items() if not e.hardware]
        self._caps: Capabilities | None = None
        self.setMinimumWidth(330)

        # header
        self.title = QLabel("No media selected")
        self.title.setObjectName("heading")
        self.title.setWordWrap(True)
        self.kind_badge = QLabel()
        self.kind_badge.setObjectName("badge")
        self.facts = muted("")
        head = QVBoxLayout()
        top = QHBoxLayout()
        top.addWidget(self.title, 1)
        top.addWidget(self.kind_badge)
        head.addLayout(top)
        head.addWidget(self.facts)

        self.sections: dict[str, Section] = {}
        body = QWidget()
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(0, 0, 6, 0)
        self.body_layout.setSpacing(0)
        self._build_transform()
        self._build_trim()
        self._build_color()
        self._build_filters()
        self._build_enhance()
        self._build_speed()
        self._build_audio()
        self._build_ai()
        self._build_export()
        self.body_layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.warning = QLabel()
        self.warning.setObjectName("warning")
        self.warning.setWordWrap(True)
        self.btn_preview = QPushButton("Preview 3 s")
        self.btn_preview.setToolTip("Render the first 3 seconds of the edited clip to check motion, speed and audio")
        self.btn_queue = QPushButton("Export")
        self.btn_queue.setObjectName("primary")
        self.btn_queue.setIcon(icon("download", "#ffffff"))
        self.btn_preview.clicked.connect(lambda: self._item and self.preview_requested.emit(self._item.id))
        self.btn_queue.clicked.connect(lambda: self._item and self.queue_requested.emit(self._item.id))
        foot = QHBoxLayout()
        foot.addWidget(self.btn_preview)
        foot.addWidget(self.btn_queue, 1)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 6, 10)
        lay.addLayout(head)
        lay.addWidget(scroll, 1)
        lay.addWidget(self.warning)
        lay.addLayout(foot)
        self.set_item(None)

    # ------------------------------------------------------------------------------- building
    def _section(self, key: str, title: str, icon_name: str, expanded: bool = False, ai: bool = False) -> Section:
        s = Section(title, icon_name, expanded, ai)
        self.sections[key] = s
        self.body_layout.addWidget(s)
        return s

    def _slider(self, section: Section, sec_key: str, field: str, label: str, lo, hi, default, step=1.0, unit="",
                tip="", ai=False) -> SliderRow:
        row = SliderRow(label, lo, hi, default, step, unit, tip, ai)
        row.previewed.connect(lambda v, f=field, s=sec_key: self._preview(s, **{f: v}))
        row.committed.connect(lambda v, f=field, s=sec_key: self._emit(s, **{f: v}))
        section.add(row)
        return row

    def _build_transform(self) -> None:
        s = self._section("transform", "Transform", "crop", expanded=True)
        self.btn_rot_l = tool_button("rotate-ccw", "Rotate 90° counter-clockwise", lambda: self._rotate(-90))
        self.btn_rot_r = tool_button("rotate-cw", "Rotate 90° clockwise", lambda: self._rotate(90))
        self.btn_rot_180 = QPushButton("180°")
        self.btn_rot_180.setToolTip("Rotate 180°")
        self.btn_rot_180.clicked.connect(lambda: self._rotate(180))
        self.btn_flip_h = tool_button("flip-horizontal-2", "Flip horizontally (mirror)", checkable=True)
        self.btn_flip_v = tool_button("flip-vertical-2", "Flip vertically", checkable=True)
        self.btn_flip_h.toggled.connect(lambda v: self._emit("transform", flip_h=v))
        self.btn_flip_v.toggled.connect(lambda v: self._emit("transform", flip_v=v))
        self.lbl_rot = muted("", wrap=False)
        s.add_row(self.btn_rot_l, self.btn_rot_r, self.btn_rot_180, self.btn_flip_h, self.btn_flip_v, self.lbl_rot)

        self.btn_crop = QPushButton("Crop")
        self.btn_crop.setIcon(icon("crop", theme.TEXT, active="#ffffff"))
        self.btn_crop.setCheckable(True)
        self.btn_crop.setToolTip("Draw the crop rectangle on the preview. Handles resize it, dragging inside moves it.")
        self.btn_crop.toggled.connect(self.crop_tool_toggled)
        self.cmb_ratio = _combo([(("Freeform" if r == "free" else r), r) for r in CROP_RATIOS])
        self.cmb_ratio.setToolTip("Keep the crop at this aspect ratio")
        self.cmb_ratio.currentIndexChanged.connect(lambda _i: self._ratio_changed())
        self.btn_crop_reset = tool_button("refresh-ccw", "Remove the crop", lambda: self._emit("transform", crop=None))
        s.add_row(self.btn_crop, self.cmb_ratio, self.btn_crop_reset)
        self.btn_bars = QPushButton("Detect black bars")
        self.btn_bars.setIcon(icon("scan", theme.TEXT))
        self.btn_bars.setToolTip("Find black borders and suggest a crop. Nothing changes until you apply it.")
        self.btn_bars.clicked.connect(lambda: self._item and self.detect_bars_requested.emit(self._item.id))
        self.lbl_crop = muted("", wrap=False)
        s.add_row(self.btn_bars, self.lbl_crop)
        self.suggest_box = QWidget()
        sb = QHBoxLayout(self.suggest_box)
        sb.setContentsMargins(0, 0, 0, 0)
        self.lbl_suggest = QLabel()
        self.lbl_suggest.setObjectName("warning")
        self.lbl_suggest.setWordWrap(True)
        b_apply = QPushButton("Apply crop")
        b_apply.setObjectName("primary")
        b_apply.clicked.connect(lambda: self._item and self.suggestion_accepted.emit(self._item.id))
        b_dismiss = QPushButton("Dismiss")
        b_dismiss.clicked.connect(lambda: self._item and self.suggestion_dismissed.emit(self._item.id))
        sb.addWidget(self.lbl_suggest, 1)
        sb.addWidget(b_apply)
        sb.addWidget(b_dismiss)
        self.suggest_box.setVisible(False)
        s.add(self.suggest_box)

        self.cmb_align = _combo(ALIGN[:3])
        self.cmb_align.setToolTip("How the picture fills an export size with a different shape. "
                                  "Fit adds bars, Fill crops the edges, Blur fill puts it on a blurred copy of itself.")
        self.cmb_align.currentIndexChanged.connect(lambda _i: self._emit("transform", alignment=self.cmb_align.currentData()))
        self.chk_stretch = QCheckBox("Allow stretch (advanced)")
        self.chk_stretch.setToolTip("Stretching distorts the picture: circles become ovals. Only for special cases.")
        self.chk_stretch.toggled.connect(self._toggle_stretch)
        self.lbl_stretch = QLabel("Stretch distorts the picture to fill the export size.")
        self.lbl_stretch.setObjectName("warning")
        self.lbl_stretch.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Alignment", self.cmb_align)
        s.body_layout.addLayout(form)
        s.add(self.chk_stretch)
        s.add(self.lbl_stretch)

    def _build_trim(self) -> None:
        s = self._section("trim", "Trim", "scissors", expanded=True)
        self.trim_start, self.trim_end = TimeEdit(), TimeEdit()
        self.trim_start.committed.connect(lambda v: self._emit("trim", start=v))
        self.trim_end.committed.connect(lambda v: self._emit("trim", end=v))
        b_in = tool_button("skip-back", "Set start to the playhead", lambda: self._playhead("start"))
        b_out = tool_button("skip-forward", "Set end to the playhead", lambda: self._playhead("end"))
        form = QFormLayout()
        r1 = QHBoxLayout()
        r1.addWidget(self.trim_start)
        r1.addWidget(b_in)
        r1.addStretch()
        r2 = QHBoxLayout()
        r2.addWidget(self.trim_end)
        r2.addWidget(b_out)
        r2.addStretch()
        form.addRow("Start", r1)
        form.addRow("End", r2)
        s.body_layout.addLayout(form)
        self.lbl_trim = muted("")
        self.btn_trim_play = QPushButton("Play selection")
        self.btn_trim_play.setIcon(icon("play", theme.TEXT))
        self.btn_trim_play.clicked.connect(lambda: self._item and self.trim_preview_requested.emit(self._item.id))
        self.btn_trim_reset = QPushButton("Reset")
        self.btn_trim_reset.clicked.connect(lambda: self._item and self.edit_replaced.emit(
            self._item.id, self._item.edit.reset_section("trim"), "Reset trim"))
        s.add_row(self.btn_trim_play, self.btn_trim_reset)
        s.add(self.lbl_trim)

    def _build_color(self) -> None:
        s = self._section("color", "Colour", "palette", expanded=True)
        self.color_rows: dict[str, SliderRow] = {}
        for spec in COLOR_BASIC:
            self.color_rows[spec.name] = self._slider(s, "color", spec.name, spec.label, spec.lo, spec.hi, spec.default,
                                                      spec.step, spec.unit, spec.tip)
        adv = Section("Advanced", "", expanded=False)
        s.add(adv)
        for spec in COLOR_ADVANCED:
            self.color_rows[spec.name] = self._slider(adv, "color", spec.name, spec.label, spec.lo, spec.hi,
                                                      spec.default, spec.step, spec.unit, spec.tip)
        b = QPushButton("Reset all colour")
        b.clicked.connect(lambda: self._item and self.edit_replaced.emit(
            self._item.id, self._item.edit.reset_section("color"), "Reset colour"))
        s.add_row(b)

    def _build_filters(self) -> None:
        s = self._section("filter", "Filters", "sparkles")
        self.cmb_look = _combo([(v["label"], k) for k, v in LOOKS.items()])
        self.cmb_look.setToolTip("A look made of real colour adjustments, added on top of your own settings")
        self.cmb_look.currentIndexChanged.connect(lambda _i: self._emit("filter", preset=self.cmb_look.currentData()))
        form = QFormLayout()
        form.addRow("Look", self.cmb_look)
        s.body_layout.addLayout(form)
        self.look_strength = self._slider(s, "filter", "strength", "Strength", 0, 100, 100, 1, "%",
                                          "How strongly the look is applied. 50% is exactly half the effect.")

    def _build_enhance(self) -> None:
        s = self._section("enhance", "Enhance", "wand-sparkles")
        self.cmb_denoise = _combo([("Off", "off"), ("Fast (hqdn3d)", "fast"), ("Quality (nlmeans, slow)", "quality")])
        self.cmb_denoise.setToolTip("Removes grain and sensor noise with ffmpeg filters (not AI)")
        self.cmb_denoise.currentIndexChanged.connect(lambda _i: self._emit("enhance", denoise=self.cmb_denoise.currentData()))
        self.cmb_deint = _combo([("Off", "off"), ("yadif", "yadif"), ("bwdif (sharper)", "bwdif")])
        self.cmb_deint.setToolTip("Removes comb-like lines from interlaced camcorder or TV recordings")
        self.cmb_deint.currentIndexChanged.connect(lambda _i: self._emit("enhance", deinterlace=self.cmb_deint.currentData()))
        form = QFormLayout()
        form.addRow("Denoise", self.cmb_denoise)
        self.lbl_deint = QLabel("Deinterlace")
        form.addRow(self.lbl_deint, self.cmb_deint)
        s.body_layout.addLayout(form)
        self.row_denoise = self._slider(s, "enhance", "denoise_strength", "Strength", 0, 100, 40, 1, "",
                                        "Higher removes more noise but also fine detail")
        self.row_sharpen = self._slider(s, "enhance", "sharpen", "Sharpen", 0, 100, 0, 1, "",
                                        "Contrast-adaptive sharpening, capped to avoid halos")
        self.row_blur = self._slider(s, "enhance", "blur", "Blur", 0, 100, 0, 1, "", "Gaussian blur of the whole picture")
        self.chk_stab = QCheckBox("Stabilize (two-pass)")
        self.chk_stab.setToolTip("Measures camera shake, then smooths it out. Shows in rendered previews, not in still frames.")
        self.chk_stab.toggled.connect(lambda v: self._emit("enhance", stabilize=v))
        s.add(self.chk_stab)
        self.row_smooth = self._slider(s, "enhance", "stab_smoothing", "Smoothing", 1, 60, 15, 1, " fr",
                                       "How many frames the camera path is smoothed over")
        self.row_zoom = self._slider(s, "enhance", "stab_zoom", "Zoom", 0, 20, 5, 0.5, "%",
                                     "Zooms in slightly to hide moving borders (crop compensation)")

    def _build_speed(self) -> None:
        s = self._section("speed", "Speed", "gauge")
        row = QHBoxLayout()
        row.setSpacing(3)
        self.speed_buttons: dict[float, QPushButton] = {}
        for r in SPEED_PRESETS:
            b = QPushButton(f"{r:g}×")
            b.setCheckable(True)
            b.setFixedWidth(42)
            b.clicked.connect(lambda _c=False, rate=r: self._emit("speed", rate=rate))
            self.speed_buttons[r] = b
            row.addWidget(b)
        row.addStretch()
        s.body_layout.addLayout(row)
        self.spin_speed = QDoubleSpinBox()
        self.spin_speed.setRange(0.25, 4.0)
        self.spin_speed.setDecimals(3)
        self.spin_speed.setSingleStep(0.05)
        self.spin_speed.setSuffix(" ×")
        self.spin_speed.editingFinished.connect(lambda: self._emit("speed", rate=self.spin_speed.value()))
        self.chk_reverse = QCheckBox("Reverse")
        self.chk_reverse.setToolTip("Plays the clip backwards (video and audio). Long clips need a lot of memory.")
        self.chk_reverse.toggled.connect(lambda v: self._emit("speed", reverse=v))
        s.add_row(QLabel("Custom"), self.spin_speed, self.chk_reverse)
        self.lbl_speed = muted("")
        s.add(self.lbl_speed)

    def _build_audio(self) -> None:
        s = self._section("audio", "Audio", "audio-lines")
        self.chk_mute = QCheckBox("Mute (export without sound)")
        self.chk_mute.toggled.connect(lambda v: self._emit("audio", mute=v))
        s.add(self.chk_mute)
        self.row_volume = self._slider(s, "audio", "volume_db", "Volume", -30, 12, 0, 0.5, " dB",
                                       "Raise or lower the loudness. +6 dB is about twice as loud.")
        self.chk_norm = QCheckBox("Normalize loudness (−16 LUFS)")
        self.chk_norm.setToolTip("Evens out loudness to a standard level used by most platforms")
        self.chk_norm.toggled.connect(lambda v: self._emit("audio", normalize=v))
        s.add(self.chk_norm)
        self.row_fade_in = self._slider(s, "audio", "fade_in", "Fade in", 0, 10, 0, 0.1, " s", "Fade the sound in at the start")
        self.row_fade_out = self._slider(s, "audio", "fade_out", "Fade out", 0, 10, 0, 0.1, " s", "Fade the sound out at the end")
        self.btn_extract = QPushButton("Export audio only")
        self.btn_extract.setIcon(icon("music", theme.TEXT))
        self.btn_extract.setToolTip("Save just the soundtrack, with these audio settings, in the audio format chosen under Export")
        self.btn_extract.clicked.connect(lambda: self._item and self.audio_export_requested.emit(self._item.id))
        s.add_row(self.btn_extract)

    def _build_ai(self) -> None:
        s = self._section("ai", "AI tools", "sparkles", expanded=True, ai=True)
        self.chk_upscale = QCheckBox("AI upscale (Real-ESRGAN)")
        self.chk_upscale.setProperty("role", "ai")
        self.chk_upscale.toggled.connect(lambda v: self._emit("enhance", upscale=v))
        s.add(self.chk_upscale)
        self.cmb_preset = QComboBox()
        for p in PRESETS.values():
            self.cmb_preset.addItem(p.label, p.key)
        self.cmb_preset.currentIndexChanged.connect(lambda _i: self._emit("enhance", preset=self.cmb_preset.currentData()))
        self.lbl_preset = muted("")
        self.cmb_target = QComboBox()
        self.cmb_target.addItems(TARGET_CHOICES)
        self.cmb_target.currentTextChanged.connect(lambda t: self._emit("enhance", target=t))
        self.lbl_size = QLabel()
        form = QFormLayout()
        form.addRow("Content", self.cmb_preset)
        form.addRow("", self.lbl_preset)
        form.addRow("Upscale to", self.cmb_target)
        form.addRow("Output size", self.lbl_size)
        s.body_layout.addLayout(form)
        self.lbl_ai_status = muted("")
        s.add(self.lbl_ai_status)

    def _build_export(self) -> None:
        s = self._section("export", "Export", "download", expanded=True)
        self.cmb_platform = _combo([(p.label, k) for k, p in EXPORT_PRESETS.items()])
        self.cmb_platform.setToolTip("Apply recommended settings for a platform; everything stays editable")
        self.cmb_platform.currentIndexChanged.connect(lambda _i: self._apply_platform())
        self.lbl_platform = muted("")
        self.cmb_format = QComboBox()
        self.cmb_format.currentIndexChanged.connect(lambda _i: self._format_changed())
        self.cmb_encoder = QComboBox()
        self.cmb_encoder.currentIndexChanged.connect(
            lambda _i: self._emit("export", video_codec=self.cmb_encoder.currentData()) if self.cmb_encoder.currentData() else None)
        self.cmb_res = _combo([(lbl, (w, h)) for lbl, w, h in RESOLUTIONS])
        self.cmb_res.currentIndexChanged.connect(lambda _i: self._res_changed())
        self.spin_w, self.spin_h = QSpinBox(), QSpinBox()
        for sp in (self.spin_w, self.spin_h):
            sp.setRange(16, 7680)
            sp.setSingleStep(2)
            sp.editingFinished.connect(self._custom_size)
        self.custom_row = QWidget()
        cr = QHBoxLayout(self.custom_row)
        cr.setContentsMargins(0, 0, 0, 0)
        cr.addWidget(self.spin_w)
        cr.addWidget(QLabel("×"))
        cr.addWidget(self.spin_h)
        self.cmb_fps = _combo(FPS_CHOICES)
        self.cmb_fps.currentIndexChanged.connect(lambda _i: self._emit("export", fps=self.cmb_fps.currentData()))
        self.cmb_rate = _combo([("Constant quality", "quality"), ("Bitrate", "bitrate"), ("Target file size", "size")])
        self.cmb_rate.currentIndexChanged.connect(lambda _i: self._emit("export", rate_control=self.cmb_rate.currentData()))
        self.sld_quality = QSlider(Qt.Horizontal)
        self.sld_quality.setRange(*QUALITY_RANGE)
        self.sld_quality.setInvertedAppearance(True)  # right = better (lower CRF)
        self.lbl_quality = QLabel()
        self.lbl_quality.setMinimumWidth(140)
        self.quality_row = QWidget()
        qr = QHBoxLayout(self.quality_row)
        qr.setContentsMargins(0, 0, 0, 0)
        qr.addWidget(self.sld_quality, 1)
        qr.addWidget(self.lbl_quality)
        self.sld_quality.valueChanged.connect(self._quality_moved)
        self.sld_quality.sliderReleased.connect(lambda: self._emit("export", quality=self.sld_quality.value()))
        self.spin_bitrate = QSpinBox()
        self.spin_bitrate.setRange(100, 200000)
        self.spin_bitrate.setSuffix(" kb/s")
        self.spin_bitrate.editingFinished.connect(lambda: self._emit("export", video_bitrate=self.spin_bitrate.value()))
        self.spin_mb = QDoubleSpinBox()
        self.spin_mb.setRange(0.1, 100000)
        self.spin_mb.setDecimals(1)
        self.spin_mb.setSuffix(" MB")
        self.spin_mb.editingFinished.connect(lambda: self._emit("export", target_mb=self.spin_mb.value()))
        self.lbl_estimate = muted("")
        self.cmb_abitrate = _combo([("96 kb/s", 96), ("128 kb/s", 128), ("192 kb/s", 192), ("256 kb/s", 256), ("320 kb/s", 320)])
        self.cmb_abitrate.currentIndexChanged.connect(lambda _i: self._emit("export", audio_bitrate=self.cmb_abitrate.currentData()))
        self.img_quality = self._slider(s, "export", "image_quality", "Quality", 1, 100, 92, 1, "",
                                        "JPEG/WebP quality. 100 makes WebP lossless.")
        s.body_layout.removeWidget(self.img_quality)
        self.txt_output = QLineEdit()
        self.txt_output.setPlaceholderText("Automatic (next to the source)")
        self.txt_output.editingFinished.connect(lambda: self._emit("export", output=self.txt_output.text().strip()))
        browse = QPushButton("Browse")
        browse.clicked.connect(self._browse_output)
        orow = QHBoxLayout()
        orow.addWidget(self.txt_output, 1)
        orow.addWidget(browse)
        self.export_form = form = QFormLayout()
        form.addRow("Preset", self.cmb_platform)
        form.addRow("", self.lbl_platform)
        form.addRow("Format", self.cmb_format)
        self.lbl_encoder = QLabel("Codec")
        form.addRow(self.lbl_encoder, self.cmb_encoder)
        self.lbl_res = QLabel("Size")
        form.addRow(self.lbl_res, self.cmb_res)
        form.addRow("", self.custom_row)
        self.lbl_fps = QLabel("Frame rate")
        form.addRow(self.lbl_fps, self.cmb_fps)
        self.lbl_rate = QLabel("Rate control")
        form.addRow(self.lbl_rate, self.cmb_rate)
        self.lbl_q = QLabel("Quality")
        form.addRow(self.lbl_q, self.quality_row)
        self.lbl_br = QLabel("Bitrate")
        form.addRow(self.lbl_br, self.spin_bitrate)
        self.lbl_mb = QLabel("Target")
        form.addRow(self.lbl_mb, self.spin_mb)
        form.addRow("", self.lbl_estimate)
        self.lbl_ab = QLabel("Audio")
        form.addRow(self.lbl_ab, self.cmb_abitrate)
        form.addRow(self.img_quality)
        form.addRow("Save as", orow)
        s.body_layout.addLayout(form)

    # ------------------------------------------------------------------------------- public
    def set_encoders(self, keys: list[str]) -> None:
        self._encoders = keys or ["libx264"]
        self.set_item(self._item)

    def set_capabilities(self, caps: Capabilities | None) -> None:
        self._caps = caps
        self.set_item(self._item)

    def show_suggestion(self, text: str | None) -> None:
        """Show (or hide, with None) a pending black-bar crop that the user must confirm."""
        self.lbl_suggest.setText(text or "")
        self.suggest_box.setVisible(bool(text))

    def set_crop_checked(self, on: bool) -> None:
        self.btn_crop.blockSignals(True)
        self.btn_crop.setChecked(on)
        self.btn_crop.blockSignals(False)

    def current_ratio(self) -> Fraction | None:
        r = self.cmb_ratio.currentData()
        if r in (None, "free"):
            return None
        a, b = r.split(":")
        return Fraction(int(a), int(b))

    def set_item(self, item: MediaItem | None) -> None:
        self._item = item
        self._loading = True
        try:
            self._load(item)
        finally:
            self._loading = False

    # ------------------------------------------------------------------------------- loading
    def _kind(self) -> str:
        item = self._item
        return (item.kind or "video") if item else "video"

    def _load(self, item: MediaItem | None) -> None:
        enabled = item is not None
        for sec in self.sections.values():
            sec.setEnabled(enabled)
        self.btn_preview.setEnabled(enabled)
        self.btn_queue.setEnabled(enabled)
        if item is None:
            self.title.setText("No media selected")
            self.kind_badge.setText("")
            self.facts.setText("Import media, then select it to edit.")
            self.warning.clear()
            return
        kind = self._kind()
        is_image, is_audio = kind == IMAGE, kind == AUDIO
        has_video = item.info.has_video if item.info else not is_audio
        has_audio = item.info.has_audio if item.info else not is_image
        e = item.edit
        self.title.setText(item.name)
        self.kind_badge.setText({"video": "VIDEO", "image": "IMAGE", "audio": "AUDIO", "gif": "GIF",
                                 "sequence": "SEQUENCE"}.get(kind, kind.upper()))
        if item.info:
            i = item.info
            if is_image:
                self.facts.setText(f"{i.resolution} · {i.video_codec} · {format_size(i.size_bytes)}")
            elif is_audio:
                self.facts.setText(f"{format_duration(i.duration)} · {i.audio_codec} · {format_size(i.size_bytes)}")
            else:
                audio = i.audio_codec or "no audio"
                self.facts.setText(f"{i.resolution} · {i.fps:.3g} fps · {format_duration(i.duration)}\n"
                                   f"{i.video_codec} / {audio} · {format_size(i.size_bytes)}")
        else:
            self.facts.setText("Reading file information…" if item.exists() else "File missing")

        visible = {"transform": has_video, "trim": not is_image, "color": has_video, "filter": has_video,
                   "enhance": has_video, "speed": not is_image, "audio": has_audio and not is_image,
                   "ai": has_video, "export": True}
        for key, sec in self.sections.items():
            sec.setVisible(visible[key])
        self.btn_preview.setVisible(not is_image)
        self.btn_queue.setText("Export image" if is_image else "Export audio" if is_audio else "Export")

        t = e.transform
        self.lbl_rot.setText(f"{t.rotate}°" + (" · mirrored" if t.flip_h else "") + (" · flipped" if t.flip_v else ""))
        self.btn_flip_h.setChecked(t.flip_h)
        self.btn_flip_v.setChecked(t.flip_v)
        _set_combo(self.cmb_ratio, t.crop_ratio)
        self.lbl_crop.setText(f"Crop {t.crop.w}×{t.crop.h} at {t.crop.x},{t.crop.y}" if t.crop else "No crop")
        self.btn_crop_reset.setEnabled(t.crop is not None)
        self.chk_stretch.setChecked(t.alignment == "stretch" or self.cmb_align.count() == 4)
        self._toggle_stretch(self.chk_stretch.isChecked(), emit=False)
        _set_combo(self.cmb_align, t.alignment)
        self.lbl_stretch.setVisible(t.alignment == "stretch")

        if not is_image and item.info:
            dur = item.info.duration
            self.trim_start.set_range_max(dur)
            self.trim_end.set_range_max(dur)
            start, end = e.trim.clamp_to(dur)
            self.trim_start.set_seconds(start)
            self.trim_end.set_seconds(end)
            rate = float(exact_rate(e.speed.rate))
            self.lbl_trim.setText(f"Selection {format_duration(end - start)} of {format_duration(dur)}"
                                  if not e.trim.is_default else "Whole clip")
            self.lbl_speed.setText(f"Output length ≈ {format_duration((end - start) / rate)}"
                                   + (" · reversed" if e.speed.reverse else ""))

        for name, row in self.color_rows.items():
            row.set_value(getattr(e.color, name))
        _set_combo(self.cmb_look, e.filter.preset)
        self.look_strength.set_value(e.filter.strength)
        self.look_strength.setEnabled(e.filter.preset != "none")

        en = e.enhance
        _set_combo(self.cmb_denoise, en.denoise)
        _set_combo(self.cmb_deint, en.deinterlace)
        self.cmb_deint.setVisible(not is_image)
        self.lbl_deint.setVisible(not is_image)
        self.row_denoise.set_value(en.denoise_strength)
        self.row_denoise.setVisible(en.denoise != "off")
        self.row_sharpen.set_value(en.sharpen)
        self.row_blur.set_value(en.blur)
        self.chk_stab.setChecked(en.stabilize)
        for w in (self.chk_stab, self.row_smooth, self.row_zoom):
            w.setVisible(not is_image)
        self.row_smooth.set_value(en.stab_smoothing)
        self.row_zoom.set_value(en.stab_zoom)
        self.row_smooth.setEnabled(en.stabilize)
        self.row_zoom.setEnabled(en.stabilize)

        for r, b in self.speed_buttons.items():
            b.setChecked(abs(e.speed.rate - r) < 1e-6)
        self.spin_speed.setValue(e.speed.rate)
        self.chk_reverse.setChecked(e.speed.reverse)

        a = e.audio
        self.chk_mute.setChecked(a.mute)
        self.chk_mute.setVisible(not is_audio)
        self.row_volume.set_value(a.volume_db)
        self.chk_norm.setChecked(a.normalize)
        self.row_fade_in.set_value(a.fade_in)
        self.row_fade_out.set_value(a.fade_out)
        self.btn_extract.setVisible(not is_audio)

        self.chk_upscale.setChecked(en.upscale)
        self.cmb_preset.setCurrentIndex(max(0, self.cmb_preset.findData(en.preset)))
        self.cmb_target.setCurrentText(en.target)
        for w in (self.cmb_preset, self.cmb_target):
            w.setEnabled(en.upscale)

        self._load_export(item, kind)
        self._apply_capabilities(kind)
        self._refresh_derived()

    def _load_export(self, item: MediaItem, kind: str) -> None:
        x = item.edit.export
        is_image, is_audio = kind == IMAGE, kind == AUDIO
        _set_combo(self.cmb_platform, x.preset_name)
        self.lbl_platform.setText(EXPORT_PRESETS.get(x.preset_name, EXPORT_PRESETS["custom"]).note)
        self.cmb_platform.setVisible(not is_image and not is_audio)
        self.lbl_platform.setVisible(not is_image and not is_audio)
        self.cmb_format.clear()
        formats = IMAGE_FORMATS if is_image else AUDIO_FORMATS if is_audio else VIDEO_FORMATS
        for label, key in formats:
            self.cmb_format.addItem(label, key)
        _set_combo(self.cmb_format, x.image_format if is_image else x.audio_format if is_audio else x.format)
        video = not is_image and not is_audio
        gif = video and x.format == "gif"
        self.cmb_encoder.clear()
        allowed = FORMAT_ENCODERS.get(x.format, ())
        keys = [k for k in allowed if k in self._encoders]
        if x.video_codec in allowed and x.video_codec not in keys:
            keys.append(x.video_codec)
        for k in keys:
            label = ENCODERS[k].label + ("" if k in self._encoders else " (not available)")
            self.cmb_encoder.addItem(label, k)
        _set_combo(self.cmb_encoder, x.video_codec)
        size = (x.width, x.height)
        idx = next((i for i, (_l, w, h) in enumerate(RESOLUTIONS) if (w, h) == size), None)
        self.cmb_res.setCurrentIndex(idx if idx is not None else len(RESOLUTIONS) - 1)
        custom = idx is None
        self.custom_row.setVisible(custom)
        if x.width:
            self.spin_w.setValue(x.width)
            self.spin_h.setValue(x.height)
        elif item.info and item.info.has_video:
            self.spin_w.setValue(item.info.width - item.info.width % 2)
            self.spin_h.setValue(item.info.height - item.info.height % 2)
        _set_combo(self.cmb_fps, int(x.fps) if x.fps in (0, 24, 25, 30, 50, 60) else 0)
        _set_combo(self.cmb_rate, x.rate_control)
        self.sld_quality.setValue(x.quality)
        self.spin_bitrate.setValue(x.video_bitrate)
        self.spin_mb.setValue(x.target_mb)
        _set_combo(self.cmb_abitrate, x.audio_bitrate)
        self.img_quality.set_value(x.image_quality)
        self.txt_output.setText(x.output)
        rc = x.rate_control
        shows = {
            (self.lbl_encoder, self.cmb_encoder): video and not gif,
            (self.lbl_res, self.cmb_res): not is_audio,
            (self.lbl_fps, self.cmb_fps): video,
            (self.lbl_rate, self.cmb_rate): video and not gif,
            (self.lbl_q, self.quality_row): video and not gif and rc == "quality",
            (self.lbl_br, self.spin_bitrate): video and not gif and rc == "bitrate",
            (self.lbl_mb, self.spin_mb): video and not gif and rc == "size",
            (self.lbl_ab, self.cmb_abitrate): video and not gif,
        }
        for widgets, on in shows.items():
            for w in widgets:
                w.setVisible(on)
        self.lbl_estimate.setVisible(video and not gif and rc == "size")
        self.img_quality.setVisible(is_image and x.image_format in ("jpeg", "webp"))
        self.txt_output.setEnabled(video)
        self.txt_output.setPlaceholderText("Automatic (next to the source)")

    def _apply_capabilities(self, kind: str) -> None:
        caps = self._caps
        if caps is None:
            return
        gates = {
            (self.btn_rot_l, self.btn_rot_r, self.btn_rot_180, self.btn_flip_h, self.btn_flip_v): "orient",
            (self.btn_crop,): "crop", (self.cmb_denoise,): "denoise_fast", (self.cmb_deint,): "deinterlace",
            (self.chk_stab,): "stabilize", (self.chk_reverse,): "reverse", (self.row_blur,): "blur",
            (self.row_sharpen,): "sharpen", (self.cmb_look,): "looks", (self.row_volume, self.chk_norm): "audio",
        }
        status = {st.feature.key: st for st in caps.feature_status()}
        for widgets, key in gates.items():
            st = status.get(key)
            for w in widgets:
                if st is not None and not st.available:
                    w.setEnabled(False)
                    w.setToolTip(st.describe())
        if "color" in status and not status["color"].available:
            for row in self.color_rows.values():
                row.setEnabled(False)
                row.setToolTip(status["color"].describe())

    # ------------------------------------------------------------------------------- derived info
    def _refresh_derived(self) -> None:
        item = self._item
        if item is None:
            return
        e = item.edit
        kind = self._kind()
        up = e.enhance
        self.lbl_preset.setText(get_preset(up.preset).description)
        self.lbl_quality.setText(f"{e.export.quality} · {quality_note(e.export.quality)}")
        problems = []
        if not item.exists():
            problems.append("The source file is missing. Re-import it.")
        size_text = "—"
        if item.info and item.info.has_video:
            try:
                plan = build_plan(e, item.info, still=kind == IMAGE)
                w, h = plan.out_size
                size_text = f"{w}×{h}"
                if up.upscale and not kind == IMAGE and (plan.stabilize or plan.reverse):
                    problems.append("Stabilization and reverse can't be combined with AI upscaling yet.")
            except ValueError as err:
                problems.append(str(err))
        self.lbl_size.setText(size_text)
        ex = e.export
        if kind not in (IMAGE, AUDIO) and ex.format != "gif" and ex.video_codec not in self._encoders:
            problems.append("The chosen codec isn't available on this computer.")
        if ex.rate_control == "size" and item.info:
            try:
                plan = build_plan(e, item.info)
                from ..render.ffmpeg_ops import MIN_VIDEO_KBPS, target_kbps
                kbps = target_kbps(ex, plan.out_duration, item.info.has_audio and not e.audio.mute)
                self.lbl_estimate.setText(f"≈ {max(kbps, 0)} kb/s video. The final size is approximate.")
                if kbps < MIN_VIDEO_KBPS:
                    problems.append("The target size is too small for this length. Choose a larger size or a shorter trim.")
            except ValueError:
                pass
        ai_ready = self._ai_installed
        self.lbl_ai_status.setText("" if ai_ready else "The AI upscaler isn't installed yet. Tools → Download tools.")
        self.warning.setText("\n".join(problems))
        self.btn_queue.setEnabled(not problems)
        self.btn_preview.setEnabled(not problems)

    _ai_installed = True

    def set_ai_installed(self, on: bool) -> None:
        self._ai_installed = on
        self._refresh_derived()

    # ------------------------------------------------------------------------------- events
    def _emit(self, section: str, **changes) -> None:
        if not self._loading and self._item is not None:
            self.changed.emit(self._item.id, section, changes)

    def _preview(self, section: str, **changes) -> None:
        if not self._loading and self._item is not None:
            self.previewed.emit(self._item.id, section, changes)

    def _rotate(self, delta: int) -> None:
        if self._item is not None:
            self._emit("transform", rotate=(self._item.edit.transform.rotate + delta) % 360, crop=None)

    def _ratio_changed(self) -> None:
        self._emit("transform", crop_ratio=self.cmb_ratio.currentData())

    def _toggle_stretch(self, on: bool, emit: bool = True) -> None:
        has = self.cmb_align.count() == 4
        if on and not has:
            self.cmb_align.addItem(*ALIGN[3])
        elif not on and has:
            if self.cmb_align.currentData() == "stretch":
                self.cmb_align.setCurrentIndex(0)
            self.cmb_align.removeItem(3)

    def _playhead(self, which: str) -> None:
        if self._item is not None:
            self.playhead_requested.emit(self._item.id, which)

    def _quality_moved(self, v: int) -> None:
        self.lbl_quality.setText(f"{v} · {quality_note(v)}")
        if not self.sld_quality.isSliderDown():  # keyboard / click: commit immediately
            self._emit("export", quality=v)

    def _format_changed(self) -> None:
        if self._loading or self._item is None:
            return
        kind = self._kind()
        key = self.cmb_format.currentData()
        if kind == IMAGE:
            self._emit("export", image_format=key)
        elif kind == AUDIO:
            self._emit("export", audio_format=key)
        else:
            from ..tools.encoders import encoder_for_format
            codec = encoder_for_format(key, self._item.edit.export.video_codec, self._encoders)
            changes = {"format": key, "preset_name": "custom"}
            if codec:
                changes["video_codec"] = codec
            if key == "webm":
                changes["audio_codec"] = "opus"
            self._emit("export", **changes)

    def _res_changed(self) -> None:
        if self._loading:
            return
        w, h = self.cmb_res.currentData()
        if w == -1:
            self.custom_row.setVisible(True)
            self._custom_size()
            return
        self._emit("export", width=w, height=h, preset_name="custom")

    def _custom_size(self) -> None:
        if not self._loading:
            self._emit("export", width=self.spin_w.value(), height=self.spin_h.value(), preset_name="custom")

    def _apply_platform(self) -> None:
        if self._loading or self._item is None:
            return
        from ..render.export_presets import preset_settings
        export_changes, transform_changes = preset_settings(self.cmb_platform.currentData(), self._item.info)
        e = self._item.edit.with_section("export", **export_changes)
        if transform_changes:
            e = e.with_section("transform", **transform_changes)
        self.edit_replaced.emit(self._item.id, e, f"Apply {self.cmb_platform.currentText()} preset")

    def _browse_output(self) -> None:
        if self._item is None:
            return
        ext = "." + self._item.edit.export.format
        start = self.txt_output.text() or str(Path(self._item.path).with_name(Path(self._item.path).stem + "_out" + ext))
        path, _ = QFileDialog.getSaveFileName(self, "Save export as", start, f"{ext[1:].upper()} (*{ext})")
        if path:
            if not path.lower().endswith(ext):
                path += ext
            self.txt_output.setText(path)
            self._emit("export", output=path)
