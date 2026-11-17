"""Main window: Midnight Studio layout, wiring the document, preview engine and render queue together."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QPixmap
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QMainWindow, QMenuBar, QMessageBox, QSplitter,
                               QStatusBar, QTabWidget, QToolButton, QVBoxLayout, QWidget)

from .. import APP_NAME, __version__
from ..core import paths
from ..core.errors import FrameWiseError
from ..core.logging_setup import get_logger
from ..core.settings import Settings
from ..media.probe import (ALL_MEDIA_EXTENSIONS, AUDIO, AUDIO_EXTENSIONS, GIF, IMAGE, IMAGE_EXTENSIONS, SEQUENCE,
                           VIDEO_EXTENSIONS, probe_source)
from ..media.source import detect_sequence
from ..media.thumbnails import make_thumbnail
from ..project.document import ProjectDocument
from ..project.edit import Edit, Enhance, Export
from ..project.io import EXTENSION
from ..project.model import MediaItem
from ..render.analysis import detect_black_bars
from ..render.preview import PreviewRequest, original_request, render_preview
from ..render.queue import Job, JobStatus, RenderQueue
from ..render.spec import spec_from_item
from ..tools import registry
from ..tools.capabilities import Capabilities
from ..tools.encoders import detect_available
from . import theme
from .icons import icon
from .inspector import Inspector
from .media_panel import MediaPanel
from .player import Player
from .queue_panel import QueuePanel
from .settings_dialog import SettingsDialog
from .setup_dialog import SetupDialog
from .tasks import TaskRunner
from .timeline import TimelinePanel
from .widgets import Segmented, tool_button

log = get_logger("ui")
MEDIA_FILTER = ("Media (" + " ".join(f"*{e}" for e in ALL_MEDIA_EXTENSIONS) + ");;"
                "Videos (" + " ".join(f"*{e}" for e in VIDEO_EXTENSIONS) + ");;"
                "Images (" + " ".join(f"*{e}" for e in IMAGE_EXTENSIONS) + ");;"
                "Audio (" + " ".join(f"*{e}" for e in AUDIO_EXTENSIONS) + ");;All files (*)")
VIDEO_FILTER = MEDIA_FILTER  # Phase 1 name
PROJECT_FILTER = f"FrameWise project (*{EXTENSION})"
STUDIO_KINDS = {"image": {IMAGE}, "video": {"video", GIF, SEQUENCE, AUDIO}}


def studio_for(kind: str | None) -> str:
    return "image" if kind == IMAGE else "video"


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings | None = None, tools_dir: Path | None = None,
                 check_tools_on_start: bool = True) -> None:
        super().__init__()
        self.settings = settings or Settings.load(paths.settings_path())
        self.tools_dir = tools_dir or paths.tools_dir()
        self.tools = registry.locate(self.tools_dir)
        self.encoders: list[str] = [self.settings.default_encoder]
        self.capabilities: Capabilities | None = None
        self.doc = ProjectDocument()
        self.tasks = TaskRunner(self)
        self.queue = RenderQueue(lambda: registry.locate(self.tools_dir))
        self.workspace = self.settings.workspace if self.settings.workspace in STUDIO_KINDS else "video"
        self._preview_token = 0
        self._live_edit: Edit | None = None
        self._suggested_crop: dict[str, object] = {}
        self.compare_mode = "edited"

        self.media = MediaPanel()
        self.player = Player()
        self.inspector = Inspector()
        self.queue_panel = QueuePanel(self.queue)
        self.timeline = TimelinePanel()

        self._build_actions()
        self.setMenuWidget(self._build_topbar())
        self.setCentralWidget(self._build_body())
        self.setStatusBar(QStatusBar())
        self.setAcceptDrops(True)
        self.resize(1440, 900)

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(140)
        self._preview_timer.timeout.connect(self._request_preview)

        self.doc.subscribe(self._on_doc_event)
        self.media.selection_changed.connect(self._on_selection)
        self.media.import_requested.connect(self.import_media)
        self.media.remove_requested.connect(self.doc.remove_media)
        self.inspector.changed.connect(self._on_inspector_change)
        self.inspector.previewed.connect(self._on_inspector_preview)
        self.inspector.edit_replaced.connect(lambda item_id, e, text: self.doc.set_edit(item_id, e, text))
        self.inspector.preview_requested.connect(lambda i: self.enqueue([i], preview=True))
        self.inspector.queue_requested.connect(lambda i: self.enqueue([i]))
        self.inspector.audio_export_requested.connect(lambda i: self.enqueue([i], audio_only=True))
        self.inspector.crop_tool_toggled.connect(self._set_crop_tool)
        self.inspector.detect_bars_requested.connect(self._detect_bars)
        self.inspector.suggestion_accepted.connect(self._accept_suggestion)
        self.inspector.suggestion_dismissed.connect(self._dismiss_suggestion)
        self.inspector.trim_preview_requested.connect(self._play_trim)
        self.inspector.playhead_requested.connect(self._trim_to_playhead)
        self.player.position_settled.connect(lambda _t: self.schedule_preview())
        self.player.position_settled.connect(self.timeline.track.set_playhead)
        self.player.player.positionChanged.connect(lambda ms: self.timeline.track.set_playhead(ms / 1000))
        self.player.frame_view.crop_changed.connect(self._crop_drawn)
        self.timeline.track.seek_requested.connect(self.player.seek)
        self.timeline.track.trim_changed.connect(self._timeline_trim)
        self.queue_panel.job_finished.connect(self._on_job_finished)
        self._set_workspace(self.workspace)
        self._refresh_title()
        self._detect_encoders()
        if check_tools_on_start:
            QTimer.singleShot(300, self._check_tools)

    # ------------------------------------------------------------------------------- layout
    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("topbar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 6, 14, 6)
        lay.setSpacing(6)
        logo = QLabel()
        logo.setPixmap(icon("aperture", theme.ACCENT, 48).pixmap(22, 22))
        b1, b2 = QLabel("Frame"), QLabel("Wise")
        b1.setObjectName("brand")
        b2.setObjectName("brandAccent")
        self.lbl_project = QLabel()
        self.lbl_project.setObjectName("muted")
        lay.addWidget(logo)
        lay.addWidget(b1)
        lay.addWidget(b2)
        lay.addSpacing(10)
        lay.addWidget(self.menu_bar)
        lay.addSpacing(6)
        lay.addWidget(self.lbl_project)
        lay.addSpacing(12)
        for act, name in ((self.act_save, "save"), (self.act_undo, "undo-2"), (self.act_redo, "redo-2")):
            act.setIcon(icon(name, theme.TEXT))  # the button mirrors its action, so the icon lives there
            b = QToolButton()
            b.setDefaultAction(act)
            b.setToolButtonStyle(Qt.ToolButtonIconOnly)
            b.setIconSize(QSize(17, 17))
            lay.addWidget(b)
        lay.addStretch()
        self.studio_switch = Segmented([("image", "Image Studio", "image"), ("video", "Video Studio", "film")])
        self.studio_switch.changed.connect(self._set_workspace)
        lay.addWidget(self.studio_switch)
        lay.addStretch()
        self.act_import.setIcon(icon("folder-open", theme.TEXT))
        imp = QToolButton()
        imp.setDefaultAction(self.act_import)
        imp.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        lay.addWidget(imp)
        lay.addWidget(tool_button("settings", "Settings", self.show_settings, size=17))
        return bar

    def _build_body(self) -> QWidget:
        view_bar = QHBoxLayout()
        view_bar.setContentsMargins(4, 0, 4, 0)
        self.compare = Segmented([("edited", "Edited", "eye"), ("original", "Original", ""),
                                  ("split", "Split", "columns-2"), ("side", "Side by side", "square-split-horizontal")])
        self.compare.set_current("edited")
        self.compare.changed.connect(self._set_compare)
        self.btn_ai_preview = QToolButton()
        self.btn_ai_preview.setCheckable(True)
        self.btn_ai_preview.setChecked(self.settings.preview_ai)
        self.btn_ai_preview.setIcon(icon("sparkles", theme.AI, active=theme.AI))
        self.btn_ai_preview.setText("AI in preview")
        self.btn_ai_preview.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.btn_ai_preview.setToolTip("Run the AI upscaler on preview frames too (takes a few seconds per frame)")
        self.btn_ai_preview.toggled.connect(lambda _v: self.schedule_preview(immediate=True))
        self.lbl_view = QLabel()
        self.lbl_view.setObjectName("muted")
        view_bar.addWidget(self.compare)
        view_bar.addSpacing(10)
        view_bar.addWidget(self.btn_ai_preview)
        view_bar.addStretch()
        view_bar.addWidget(self.lbl_view)
        center = QWidget()
        center.setObjectName("panel")
        cl = QVBoxLayout(center)
        cl.setContentsMargins(8, 8, 8, 8)
        cl.addLayout(view_bar)
        cl.addWidget(self.player, 1)

        left, right = QWidget(), QWidget()
        for w, inner in ((left, self.media), (right, self.inspector)):
            w.setObjectName("panel")
            lo = QVBoxLayout(w)
            lo.setContentsMargins(4, 4, 4, 4)
            lo.addWidget(inner)

        self.bottom = QTabWidget()
        self.bottom.addTab(self.timeline, icon("list-video", theme.MUTED), "Timeline")
        self.bottom.addTab(self.queue_panel, icon("download", theme.MUTED), "Render queue")
        bottom = QWidget()
        bottom.setObjectName("panel")
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(4, 4, 4, 4)
        bl.addWidget(self.bottom)

        top = QSplitter(Qt.Horizontal)
        top.addWidget(left)
        top.addWidget(center)
        top.addWidget(right)
        top.setStretchFactor(1, 1)
        top.setSizes([280, 760, 400])
        main = QSplitter(Qt.Vertical)
        main.addWidget(top)
        main.addWidget(bottom)
        main.setStretchFactor(0, 1)
        main.setSizes([640, 190])
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(8, 8, 8, 8)
        wl.addWidget(main)
        return wrap

    # ------------------------------------------------------------------------------- actions / menus
    def _action(self, text: str, slot, shortcut=None, tip: str = "") -> QAction:
        a = QAction(text, self)
        if shortcut is not None:
            a.setShortcut(QKeySequence(shortcut))
        if tip:
            a.setStatusTip(tip)
            a.setToolTip(tip)
        a.triggered.connect(slot)
        return a

    def _build_actions(self) -> None:
        self.act_new = self._action("&New project", self.new_project, QKeySequence.New)
        self.act_open = self._action("&Open project…", self.open_project_dialog, QKeySequence.Open)
        self.act_save = self._action("&Save project", self.save_project, QKeySequence.Save, "Save project (Ctrl+S)")
        self.act_save_as = self._action("Save project &as…", self.save_project_as, QKeySequence.SaveAs)
        self.act_import = self._action("&Import media…", self.import_media, "Ctrl+I", "Import videos, images or audio")
        self.act_import_seq = self._action("Import image &sequence…", self.import_sequence)
        self.act_quit = self._action("&Quit", self.close, QKeySequence.Quit)
        self.act_undo = self._action("&Undo", self.doc.undo.undo, QKeySequence.Undo, "Undo")
        self.act_redo = self._action("&Redo", self.doc.undo.redo, QKeySequence.Redo, "Redo")
        self.act_remove = self._action("&Remove from project",
                                       lambda: self.doc.remove_media(self.media.selected_ids()), QKeySequence.Delete)
        self.act_preview = self._action("&Preview selected (3 s)",
                                        lambda: self.enqueue(self.media.selected_ids(), preview=True), "Ctrl+P")
        self.act_render_sel = self._action("&Export selected", lambda: self.enqueue(self.media.selected_ids()), "Ctrl+R")
        self.act_render_all = self._action("Export &all",
                                           lambda: self.enqueue([i.id for i in self.doc.project.items]), "Ctrl+Shift+R")
        self.act_tools = self._action("&Download / check tools…", self.show_setup)
        self.act_settings = self._action("&Settings…", self.show_settings, "Ctrl+,")
        self.act_logs = self._action("Open &logs folder",
                                     lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.logs_dir()))))
        self.act_about = self._action("&About FrameWise", self._about)
        for act in (self.act_new, self.act_open, self.act_save, self.act_save_as, self.act_import, self.act_quit,
                    self.act_undo, self.act_redo, self.act_remove, self.act_preview, self.act_render_sel,
                    self.act_render_all, self.act_settings):
            self.addAction(act)  # shortcuts work wherever focus is
        mb = self.menu_bar = QMenuBar()
        mb.setNativeMenuBar(False)
        m = mb.addMenu("&File")
        for a in (self.act_new, self.act_open):
            m.addAction(a)
        self.recent_menu = m.addMenu("Open &recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        m.addSeparator()
        for a in (self.act_save, self.act_save_as, None, self.act_import, self.act_import_seq, None,
                  self.act_settings, self.act_quit):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("&Edit")
        for a in (self.act_undo, self.act_redo, self.act_remove):
            m.addAction(a)
        m = mb.addMenu("&Export")
        for a in (self.act_preview, self.act_render_sel, self.act_render_all):
            m.addAction(a)
        m = mb.addMenu("&Tools")
        m.addAction(self.act_tools)
        m.addAction(self.act_logs)
        mb.addMenu("&Help").addAction(self.act_about)
        self._refresh_actions()

    def _refresh_actions(self) -> None:
        u = self.doc.undo
        self.act_undo.setEnabled(u.can_undo())
        self.act_redo.setEnabled(u.can_redo())
        self.act_undo.setToolTip(f"Undo {u.undo_text()}".rstrip())
        self.act_redo.setToolTip(f"Redo {u.redo_text()}".rstrip())
        has_sel = bool(self.media.selected_ids())
        for a in (self.act_remove, self.act_preview, self.act_render_sel):
            a.setEnabled(has_sel)
        self.act_render_all.setEnabled(bool(self.doc.project.items))

    def _refresh_title(self) -> None:
        dirty = "*" if self.doc.dirty else ""
        self.setWindowTitle(f"{self.doc.title}{dirty} — {APP_NAME}")
        if hasattr(self, "lbl_project"):
            self.lbl_project.setText(f"{self.doc.title}{dirty}")

    # ------------------------------------------------------------------------------- workspace
    def _set_workspace(self, key: str) -> None:
        self.workspace = key if key in STUDIO_KINDS else "video"
        self.settings.workspace = self.workspace
        self.studio_switch.set_current(self.workspace)
        self.media.set_filter(STUDIO_KINDS[self.workspace])
        video = self.workspace == "video"
        self.bottom.setTabVisible(0, video)
        if not video:
            self.bottom.setCurrentIndex(1)
        self._on_selection(self.media.current_id())

    # ------------------------------------------------------------------------------- document events
    def _on_doc_event(self, event: str, payload) -> None:
        items = self.doc.project.items
        if event == "reset":
            self.media.reset(items)
            self.media.set_filter(STUDIO_KINDS[self.workspace])
            for it in items:
                self._load_media_facts(it)
            self._on_selection(None)
        elif event == "added":
            item = self.doc.project.find(payload)
            self.media.add_item(item, self.doc.project.index_of(payload))
            self._load_media_facts(item)
        elif event == "removed":
            self.media.remove_item(payload)
        elif event in ("edit", "info"):
            item = self.doc.project.find(payload)
            self.media.refresh_item(item)
            if self.media.current_id() == payload:
                self._live_edit = None
                self.inspector.set_item(item)
                self._update_timeline(item)
                if event == "info":
                    self.player.set_fps(item.info.fps if item.info else None)
                    self._load_player(item)
                self.schedule_preview()
        self._refresh_title()
        self._refresh_actions()

    def _load_media_facts(self, item: MediaItem) -> None:
        """Probe the file and make a thumbnail in the background."""
        tools = self.tools
        if tools is None or not item.exists():
            return
        path, item_id, seq = Path(item.path), item.id, item.sequence

        def work():
            info = probe_source(tools.ffprobe, path, seq)
            thumb = None
            if info.has_video:
                try:
                    thumb = make_thumbnail(tools.ffmpeg, path, paths.cache_dir() / "thumbs", info.duration)
                except FrameWiseError as e:
                    log.info("No thumbnail for %s: %s", path.name, e)
            return info, thumb

        def done(result):
            info, thumb = result
            if self.doc.project.find(item_id) is None:
                return  # removed meanwhile
            self.doc.set_info(item_id, info)
            if thumb:
                self.media.set_thumbnail(item_id, thumb)

        def failed(e):
            log.warning("Could not read %s: %s", path, e)
            self.statusBar().showMessage(f"Could not read {path.name}: {e}", 8000)

        self.tasks.run(work, done, failed)

    def current_item(self) -> MediaItem | None:
        item_id = self.media.current_id()
        return self.doc.project.find(item_id) if item_id else None

    def _on_selection(self, item_id) -> None:
        item = self.doc.project.find(item_id) if item_id else None
        self._live_edit = None
        self._set_crop_tool(False)
        self.inspector.set_crop_checked(False)
        self.inspector.show_suggestion(None)
        self.inspector.set_item(item)
        self._load_player(item)
        self._update_timeline(item)
        self.schedule_preview(immediate=True)
        self._refresh_actions()

    def _load_player(self, item: MediaItem | None) -> None:
        if item is None:
            self.player.load(None)
            return
        info = item.info
        still = (item.kind == IMAGE) or item.sequence is not None
        self.player.load(Path(item.path), info.fps if info else None, still=still,
                         has_video=info.has_video if info else item.kind != AUDIO)
        self.compare.setEnabled(not (info is not None and not info.has_video))

    def _update_timeline(self, item: MediaItem | None) -> None:
        if item is None or item.info is None or item.kind == IMAGE:
            self.timeline.set_clip(0, 0, 0)
            return
        start, end = item.edit.trim.clamp_to(item.info.duration)
        self.timeline.set_clip(item.info.duration, start, end)

    # ------------------------------------------------------------------------------- inspector events
    def _on_inspector_change(self, item_id: str, section: str, changes: dict) -> None:
        try:
            self.doc.update_edit(item_id, section, **changes)
        except ValueError as e:
            log.error("Rejected edit %s %s: %s", section, changes, e)
            self.statusBar().showMessage(str(e), 6000)

    def _on_inspector_preview(self, item_id: str, section: str, changes: dict) -> None:
        item = self.doc.project.find(item_id)
        if item is not None:
            self._live_edit = item.edit.with_section(section, **changes)
            self.schedule_preview()

    # ------------------------------------------------------------------------------- preview
    def _set_compare(self, mode: str) -> None:
        self.compare_mode = mode
        self.player.frame_view.set_mode(mode)
        self.schedule_preview(immediate=True)

    def schedule_preview(self, immediate: bool = False) -> None:
        self._preview_timer.start(0 if immediate else self._preview_timer.interval())

    def _request_preview(self) -> None:
        """Ask the preview engine for the current frame. Only the newest request's result is shown."""
        item = self.current_item()
        view = self.player.frame_view
        self._preview_token += 1
        token = self._preview_token
        if item is None or self.tools is None or item.info is None or not item.info.has_video or not item.exists():
            if item is not None and item.info is not None and not item.info.has_video:
                view.clear("Audio file: no picture")
            return
        edit = self._live_edit or item.edit
        w = self.settings.preview_width
        ai = self.btn_ai_preview.isChecked() and edit.enhance.upscale
        req = PreviewRequest(item.source, item.info, edit, time=self.player.position_seconds(), max_w=w,
                             max_h=round(w * 9 / 16) if item.info.width >= item.info.height else w,
                             include_ai=ai, without_crop=view.crop_mode)
        need_original = self.compare_mode != "edited"
        tools, cache = self.tools, paths.cache_dir() / "preview"
        view.set_busy(True)

        def work():
            edited = render_preview(req, tools, cache)
            original = render_preview(original_request(req), tools, cache) if need_original else None
            return edited, original

        def done(result):
            if token != self._preview_token:
                return  # a newer request superseded this one
            edited, original = result
            notes = []
            if edited.omitted:
                notes.append(f"Not shown in still previews: {', '.join(edited.omitted)}. Use Preview 3 s.")
            if edit.enhance.upscale and not edited.ai_applied and item.kind != AUDIO:
                notes.append("AI upscale not included in this preview (enable 'AI in preview').")
            view.set_frames(QPixmap(str(edited.image)), QPixmap(str(original.image)) if original else None,
                            edited.frame_size, "  ".join(notes))
            if view.crop_mode:
                view.set_crop_mode(True, self._crop_for_view(item), self.inspector.current_ratio())
            fw, fh = edited.frame_size
            self.lbl_view.setText(f"Output {fw}×{fh}")

        def failed(e):
            if token != self._preview_token:
                return
            view.set_busy(False)
            view.clear(f"Preview failed: {e}")
            log.warning("Preview failed: %s", e)

        self.tasks.run(work, done, failed)

    # ------------------------------------------------------------------------------- crop / bars / trim
    def _crop_for_view(self, item: MediaItem):
        suggestion = self._suggested_crop.get(item.id)
        return suggestion or item.edit.transform.crop

    def _set_crop_tool(self, on: bool) -> None:
        item = self.current_item()
        view = self.player.frame_view
        view.set_crop_mode(on and item is not None, self._crop_for_view(item) if item else None,
                           self.inspector.current_ratio())
        self.schedule_preview(immediate=True)

    def _crop_drawn(self, rect) -> None:
        item = self.current_item()
        if item is None:
            return
        if item.id in self._suggested_crop:
            self._suggested_crop[item.id] = rect  # adjusting a suggestion: still needs "Apply"
            return
        self.doc.update_edit(item.id, "transform", text="Crop", crop=rect)

    def _detect_bars(self, item_id: str) -> None:
        item = self.doc.project.find(item_id)
        if item is None or item.info is None or self.tools is None:
            return
        tools, info, transform, source = self.tools, item.info, item.edit.transform, item.source
        self.statusBar().showMessage("Looking for black bars…")

        def done(rect):
            if rect is None:
                self.statusBar().showMessage("No black bars found.", 6000)
                return
            self._suggested_crop[item_id] = rect
            self.inspector.show_suggestion(f"Suggested crop {rect.w}×{rect.h} at {rect.x},{rect.y}. "
                                           "Check it on the preview, adjust the handles, then apply.")
            self.inspector.set_crop_checked(True)
            self._set_crop_tool(True)
            self.statusBar().showMessage("Suggested crop shown on the preview (not applied yet).", 8000)

        self.tasks.run(lambda: detect_black_bars(tools.ffmpeg, source, info, transform), done,
                       lambda e: self.statusBar().showMessage(f"Black-bar detection failed: {e}", 8000))

    def _accept_suggestion(self, item_id: str) -> None:
        rect = self._suggested_crop.pop(item_id, None)
        self.inspector.show_suggestion(None)
        if rect is not None:
            self.doc.update_edit(item_id, "transform", text="Crop black bars", crop=rect)

    def _dismiss_suggestion(self, item_id: str) -> None:
        self._suggested_crop.pop(item_id, None)
        self.inspector.show_suggestion(None)
        self._set_crop_tool(self.player.frame_view.crop_mode)

    def _play_trim(self, item_id: str) -> None:
        item = self.doc.project.find(item_id)
        if item and item.info:
            start, end = item.edit.trim.clamp_to(item.info.duration)
            self.player.play_range(start, end)

    def _trim_to_playhead(self, item_id: str, which: str) -> None:
        t = round(self.player.position_seconds(), 3)
        self.doc.update_edit(item_id, "trim", text=f"Set trim {which}", **{which: t})

    def _timeline_trim(self, start: float, end: float) -> None:
        item = self.current_item()
        if item is not None and item.info is not None:
            end_value = None if end >= item.info.duration - 1e-3 else end
            self.doc.update_edit(item.id, "trim", text="Trim", start=start, end=end_value)

    # ------------------------------------------------------------------------------- media / rendering
    def import_media(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Import media", self.settings.last_open_dir, MEDIA_FILTER)
        if files:
            self.settings.last_open_dir = str(Path(files[0]).parent)
            self.add_files(files)

    def import_sequence(self) -> None:
        first, _ = QFileDialog.getOpenFileName(self, "Select the first image of the sequence",
                                               self.settings.last_open_dir,
                                               "Images (" + " ".join(f"*{e}" for e in IMAGE_EXTENSIONS) + ")")
        if not first:
            return
        seq = detect_sequence(Path(first))
        if seq is None:
            QMessageBox.information(self, APP_NAME, "No numbered sequence starts at this image "
                                    "(expected names like shot_0001.png, shot_0002.png …). It was imported as a single image.")
            self.add_files([first])
            return
        self.add_files([first], sequences={first: seq})

    def default_edit(self, path: str | None = None) -> Edit:
        enc = self.settings.default_encoder if self.settings.default_encoder in self.encoders else self.encoders[0]
        edit = Edit(export=Export(video_codec=enc, quality=self.settings.default_quality))
        if path is not None and Path(path).suffix.lower() in IMAGE_EXTENSIONS + AUDIO_EXTENSIONS:
            edit = Edit(enhance=Enhance(upscale=False), export=edit.export)  # AI upscale is opt-in for images
        return edit.validated()

    def add_files(self, files: list[str], sequences: dict | None = None) -> None:
        added = self.doc.add_media(files, self.default_edit(), sequences=sequences,
                                   defaults_for=lambda p: None if sequences and p in sequences else self.default_edit(p))
        if added:
            first = added[0]
            target = studio_for(first.kind)
            if target != self.workspace:
                self._set_workspace(target)
            self.media.select(first.id)
            self.statusBar().showMessage(f"Imported {len(added)} file(s)", 4000)
        elif files:
            self.statusBar().showMessage("Those files are already in the project", 4000)

    def enqueue(self, item_ids: list[str], preview: bool = False, audio_only: bool = False) -> list[Job]:
        if registry.locate(self.tools_dir) is None:
            self.show_setup()
            return []
        out_dir = Path(self.settings.output_dir) if self.settings.output_dir else None
        jobs, skipped = [], []
        for item_id in item_ids:
            item = self.doc.project.find(item_id)
            if item is None:
                continue
            if not item.exists():
                skipped.append(item.name)
                continue
            if preview and item.kind == IMAGE:
                continue
            spec = spec_from_item(item, preview=preview, output_dir=out_dir, audio_only=audio_only)
            jobs.append(self.queue.add(spec, item.name, preview))
        if skipped:
            QMessageBox.warning(self, APP_NAME, "These files are missing and were skipped:\n" + "\n".join(skipped))
        if jobs:
            self.bottom.setCurrentWidget(self.queue_panel)
            self.statusBar().showMessage(f"Added {len(jobs)} job(s) to the render queue", 4000)
        return jobs

    def _on_job_finished(self, job: Job) -> None:
        if job.status == JobStatus.DONE:
            self.statusBar().showMessage(f"Finished: {job.spec.output}", 10000)
            if job.preview:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(job.spec.output)))
        elif job.status == JobStatus.FAILED:
            self.statusBar().showMessage(f"Failed: {job.name}", 10000)
            if "not installed" in job.error:
                self.show_setup()
            else:
                QMessageBox.warning(self, APP_NAME, f"{job.name} failed:\n\n{job.error}")

    # ------------------------------------------------------------------------------- tools
    def _detect_encoders(self) -> None:
        """Probe ffmpeg in the background: usable encoders and supported filters."""
        tools = self.tools
        self.inspector.set_ai_installed(bool(tools and tools.upscaler))
        if tools is None:
            self.inspector.set_encoders(self.encoders)
            return

        def done(keys):
            self.encoders = keys
            self.inspector.set_encoders(keys)
            log.info("Usable encoders: %s", ", ".join(keys))

        def caps_done(caps: Capabilities):
            self.capabilities = caps
            self.inspector.set_capabilities(caps)
            problems = caps.problems()
            for p in problems:
                log.warning("Feature status: %s", p.describe())
            if problems:
                names = ", ".join(p.feature.label for p in problems)
                self.statusBar().showMessage(
                    f"Some features are limited by your ffmpeg ({names}). See Tools → Download / check tools.", 15000)

        def caps_failed(e):
            log.warning("Could not detect ffmpeg filters: %s", e)
            self.statusBar().showMessage(f"Could not check ffmpeg's features: {e}", 15000)

        self.tasks.run(lambda: detect_available(tools.ffmpeg), done)
        self.tasks.run(lambda: Capabilities.detect(tools.ffmpeg), caps_done, caps_failed)

    def _check_tools(self) -> None:
        if registry.missing_components(self.tools_dir):
            self.show_setup()

    def show_setup(self) -> None:
        SetupDialog(self.tools_dir, self, self.capabilities).exec()
        had_ffmpeg = self.tools is not None
        self.tools = registry.locate(self.tools_dir)
        self.inspector.set_ai_installed(bool(self.tools and self.tools.upscaler))
        if self.tools is not None and not had_ffmpeg:
            self._detect_encoders()
            for it in self.doc.project.items:
                if it.info is None:
                    self._load_media_facts(it)

    def show_settings(self) -> None:
        if SettingsDialog(self.settings, self.encoders, self).exec():
            self.settings.save(paths.settings_path())
            self.btn_ai_preview.setChecked(self.settings.preview_ai)
            self.schedule_preview(immediate=True)

    # ------------------------------------------------------------------------------- project files
    def _confirm_discard(self) -> bool:
        if not self.doc.dirty:
            return True
        r = QMessageBox.question(self, APP_NAME, f"Save changes to {self.doc.title}?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
        if r == QMessageBox.Save:
            return self.save_project()
        return r == QMessageBox.Discard

    def new_project(self) -> None:
        if self._confirm_discard():
            self.doc.new()
            self._refresh_title()

    def open_project_dialog(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open project", self.settings.last_open_dir, PROJECT_FILTER)
        if path:
            self.open_project(Path(path), confirmed=True)

    def open_project(self, path: Path, confirmed: bool = False) -> bool:
        if not confirmed and not self._confirm_discard():
            return False
        try:
            self.doc.open(path)
        except FrameWiseError as e:
            QMessageBox.critical(self, APP_NAME, str(e))
            return False
        self._remember(path)
        missing = [i.name for i in self.doc.project.items if not i.exists()]
        if missing:
            self.statusBar().showMessage(f"{len(missing)} file(s) in this project are missing", 10000)
        return True

    def save_project(self) -> bool:
        if self.doc.path is None:
            return self.save_project_as()
        return self._save_to(self.doc.path)

    def save_project_as(self) -> bool:
        start = str(self.doc.path or Path(self.settings.last_open_dir or Path.home()) / f"Untitled{EXTENSION}")
        path, _ = QFileDialog.getSaveFileName(self, "Save project", start, PROJECT_FILTER)
        if not path:
            return False
        if not path.endswith(EXTENSION):
            path += EXTENSION
        return self._save_to(Path(path))

    def _save_to(self, path: Path) -> bool:
        try:
            self.doc.save(path)
        except FrameWiseError as e:
            QMessageBox.critical(self, APP_NAME, str(e))
            return False
        self._remember(path)
        self.statusBar().showMessage(f"Saved {path}", 4000)
        return True

    def _remember(self, path: Path) -> None:
        self.settings.add_recent(str(path))
        self.settings.save(paths.settings_path())
        self._refresh_title()

    def _fill_recent(self) -> None:
        self.recent_menu.clear()
        recent = [p for p in self.settings.recent_projects if Path(p).exists()]
        if not recent:
            self.recent_menu.addAction("(none)").setEnabled(False)
        for p in recent:
            self.recent_menu.addAction(Path(p).name, lambda p=p: self.open_project(Path(p))).setToolTip(p)

    # ------------------------------------------------------------------------------- window events
    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        local = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        projects = [p for p in local if p.lower().endswith(EXTENSION)]
        if projects:
            self.open_project(Path(projects[0]))
            return
        media = [p for p in local if Path(p).suffix.lower() in ALL_MEDIA_EXTENSIONS]
        if media:
            self.add_files(media)
        elif local:
            self.statusBar().showMessage("Those files aren't supported media", 4000)

    def closeEvent(self, e) -> None:
        if self.queue.busy:
            r = QMessageBox.question(self, APP_NAME, "Renders are still running. Stop them and quit?\n"
                                     "AI upscales can be resumed later by adding them again.")
            if r != QMessageBox.Yes:
                e.ignore()
                return
        if not self._confirm_discard():
            e.ignore()
            return
        self._preview_token += 1
        self._preview_timer.stop()
        self.player.stop()
        self.queue.shutdown()
        self.tasks.wait(5000)
        self.settings.save(paths.settings_path())
        e.accept()

    def _about(self) -> None:
        QMessageBox.about(self, APP_NAME, f"<b>{APP_NAME}</b> {__version__}<br>"
                          "Image and video editing with local, GPU-accelerated AI tools.<br><br>"
                          "Uses ffmpeg, Real-ESRGAN (ncnn-vulkan) and Lucide icons (ISC licence).")
