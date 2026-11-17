"""Main window: wires the document, render queue and panels together."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (QFileDialog, QMainWindow, QMessageBox, QSplitter, QStatusBar, QWidget)

from .. import APP_NAME, __version__
from ..core import paths
from ..core.errors import FrameWiseError
from ..core.logging_setup import get_logger
from ..core.settings import Settings
from ..media.probe import VIDEO_EXTENSIONS, probe
from ..media.thumbnails import make_thumbnail
from ..project.document import ProjectDocument
from ..project.io import EXTENSION
from ..project.edit import Edit, Export
from ..project.model import MediaItem
from ..render.queue import Job, JobStatus, RenderQueue
from ..render.spec import spec_from_item
from ..tools import registry
from ..tools.capabilities import Capabilities
from ..tools.encoders import detect_available
from .inspector import Inspector
from .media_panel import MediaPanel
from .player import Player
from .queue_panel import QueuePanel
from .setup_dialog import SetupDialog
from .tasks import TaskRunner

log = get_logger("ui")
VIDEO_FILTER = "Videos (" + " ".join(f"*{e}" for e in VIDEO_EXTENSIONS) + ");;All files (*)"
PROJECT_FILTER = f"FrameWise project (*{EXTENSION})"


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

        self.media = MediaPanel()
        self.player = Player()
        self.inspector = Inspector()
        self.queue_panel = QueuePanel(self.queue)

        top = QSplitter(Qt.Horizontal)
        top.addWidget(self.media)
        top.addWidget(self.player)
        top.addWidget(self.inspector)
        top.setStretchFactor(1, 1)
        top.setSizes([280, 640, 340])
        main = QSplitter(Qt.Vertical)
        main.addWidget(top)
        main.addWidget(self.queue_panel)
        main.setStretchFactor(0, 1)
        main.setSizes([560, 200])
        self.setCentralWidget(main)
        self.setStatusBar(QStatusBar())
        self.setAcceptDrops(True)
        self.resize(1280, 800)

        self._build_actions()
        self.doc.subscribe(self._on_doc_event)
        self.media.selection_changed.connect(self._on_selection)
        self.media.import_requested.connect(self.import_media)
        self.media.remove_requested.connect(self.doc.remove_media)
        self.inspector.changed.connect(lambda item_id, sec, ch: self.doc.update_edit(item_id, sec, **ch))
        self.inspector.preview_requested.connect(lambda i: self.enqueue([i], preview=True))
        self.inspector.queue_requested.connect(lambda i: self.enqueue([i]))
        self.queue_panel.job_finished.connect(self._on_job_finished)
        self._refresh_title()
        self._detect_encoders()
        if check_tools_on_start:
            QTimer.singleShot(300, self._check_tools)

    # -- actions / menus ------------------------------------------------------
    def _action(self, text: str, slot, shortcut=None, tip: str = "") -> QAction:
        a = QAction(text, self)
        if shortcut is not None:
            a.setShortcut(QKeySequence(shortcut))
        if tip:
            a.setStatusTip(tip)
        a.triggered.connect(slot)
        return a

    def _build_actions(self) -> None:
        self.act_new = self._action("&New project", self.new_project, QKeySequence.New)
        self.act_open = self._action("&Open project…", self.open_project_dialog, QKeySequence.Open)
        self.act_save = self._action("&Save project", self.save_project, QKeySequence.Save)
        self.act_save_as = self._action("Save project &as…", self.save_project_as, QKeySequence.SaveAs)
        self.act_import = self._action("&Import videos…", self.import_media, "Ctrl+I")
        self.act_quit = self._action("&Quit", self.close, QKeySequence.Quit)
        self.act_undo = self._action("&Undo", self.doc.undo.undo, QKeySequence.Undo)
        self.act_redo = self._action("&Redo", self.doc.undo.redo, QKeySequence.Redo)
        self.act_remove = self._action("&Remove from project",
                                       lambda: self.doc.remove_media(self.media.selected_ids()), QKeySequence.Delete)
        self.act_preview = self._action("&Preview selected (3 s)",
                                        lambda: self.enqueue(self.media.selected_ids(), preview=True), "Ctrl+P")
        self.act_render_sel = self._action("&Render selected",
                                           lambda: self.enqueue(self.media.selected_ids()), "Ctrl+R")
        self.act_render_all = self._action("Render &all",
                                           lambda: self.enqueue([i.id for i in self.doc.project.items]),
                                           "Ctrl+Shift+R")
        self.act_tools = self._action("&Download / check tools…", self.show_setup)
        self.act_logs = self._action("Open &logs folder",
                                     lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths.logs_dir()))))
        self.act_about = self._action("&About FrameWise", self._about)

        mb = self.menuBar()
        m = mb.addMenu("&File")
        for a in (self.act_new, self.act_open):
            m.addAction(a)
        self.recent_menu = m.addMenu("Open &recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        m.addSeparator()
        for a in (self.act_save, self.act_save_as):
            m.addAction(a)
        m.addSeparator()
        m.addAction(self.act_import)
        m.addSeparator()
        m.addAction(self.act_quit)
        m = mb.addMenu("&Edit")
        for a in (self.act_undo, self.act_redo, self.act_remove):
            m.addAction(a)
        m = mb.addMenu("&Render")
        for a in (self.act_preview, self.act_render_sel, self.act_render_all):
            m.addAction(a)
        m = mb.addMenu("&Tools")
        m.addAction(self.act_tools)
        m.addAction(self.act_logs)
        mb.addMenu("&Help").addAction(self.act_about)

        tb = self.addToolBar("Main")
        tb.setObjectName("main_toolbar")
        tb.setMovable(False)
        for a in (self.act_import, None, self.act_undo, self.act_redo, None,
                  self.act_preview, self.act_render_sel, self.act_render_all):
            tb.addSeparator() if a is None else tb.addAction(a)
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
        self.setWindowTitle(f"{self.doc.title}{'*' if self.doc.dirty else ''} — {APP_NAME}")

    # -- document events ------------------------------------------------------
    def _on_doc_event(self, event: str, payload) -> None:
        items = self.doc.project.items
        if event == "reset":
            self.media.reset(items)
            for it in items:
                self._load_media_facts(it)
            self.inspector.set_item(None)
            self.player.load(None)
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
                self.inspector.set_item(item)
                if event == "info":
                    self.player.set_fps(item.info.fps if item.info else None)
        self._refresh_title()
        self._refresh_actions()

    def _load_media_facts(self, item: MediaItem) -> None:
        """Probe the file and make a thumbnail in the background."""
        tools = self.tools
        if tools is None or not item.exists():
            return
        path, item_id = Path(item.path), item.id

        def work():
            info = probe(tools.ffprobe, path)
            thumb = None
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

    def _on_selection(self, item_id) -> None:
        item = self.doc.project.find(item_id) if item_id else None
        self.inspector.set_item(item)
        self.player.load(Path(item.path) if item else None, item.info.fps if item and item.info else None)
        self._refresh_actions()

    # -- media / rendering ----------------------------------------------------
    def import_media(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Import videos", self.settings.last_open_dir, VIDEO_FILTER)
        if files:
            self.settings.last_open_dir = str(Path(files[0]).parent)
            self.add_files(files)

    def default_edit(self) -> Edit:
        enc = self.settings.default_encoder if self.settings.default_encoder in self.encoders else self.encoders[0]
        return Edit(export=Export(video_codec=enc, quality=self.settings.default_quality)).validated()

    def add_files(self, files: list[str]) -> None:
        added = self.doc.add_media(files, self.default_edit())
        if added:
            self.media.select(added[0].id)
            self.statusBar().showMessage(f"Imported {len(added)} video(s)", 4000)
        elif files:
            self.statusBar().showMessage("Those videos are already in the project", 4000)

    def enqueue(self, item_ids: list[str], preview: bool = False) -> list[Job]:
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
            jobs.append(self.queue.add(spec_from_item(item, preview=preview, output_dir=out_dir), item.name, preview))
        if skipped:
            QMessageBox.warning(self, APP_NAME, "These files are missing and were skipped:\n" + "\n".join(skipped))
        if jobs:
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

    # -- tools ------------------------------------------------------------------
    def _detect_encoders(self) -> None:
        """Probe ffmpeg in the background: usable encoders and supported filters."""
        tools = self.tools
        if tools is None:
            self.inspector.set_encoders(self.encoders)
            return

        def done(keys):
            self.encoders = keys
            self.inspector.set_encoders(keys)
            log.info("Usable encoders: %s", ", ".join(keys))

        def caps_done(caps: Capabilities):
            self.capabilities = caps
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
        if self.tools is not None and not had_ffmpeg:
            self._detect_encoders()
            for it in self.doc.project.items:
                if it.info is None:
                    self._load_media_facts(it)

    # -- project files -----------------------------------------------------------
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
            self.statusBar().showMessage(f"{len(missing)} video(s) in this project are missing", 10000)
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

    # -- window events -----------------------------------------------------------
    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e) -> None:
        local = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        projects = [p for p in local if p.lower().endswith(EXTENSION)]
        if projects:
            self.open_project(Path(projects[0]))
            return
        videos = [p for p in local if Path(p).suffix.lower() in VIDEO_EXTENSIONS]
        if videos:
            self.add_files(videos)
        elif local:
            self.statusBar().showMessage("Those files don't look like videos", 4000)

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
        self.player.stop()
        self.queue.shutdown()
        self.tasks.wait(3000)
        self.settings.save(paths.settings_path())
        e.accept()

    def _about(self) -> None:
        QMessageBox.about(self, APP_NAME, f"<b>{APP_NAME}</b> {__version__}<br>"
                          "Video editing with local, GPU-accelerated AI tools.<br><br>"
                          "Uses ffmpeg and Real-ESRGAN (ncnn-vulkan).")
