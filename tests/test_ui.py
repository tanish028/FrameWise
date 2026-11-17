"""End-to-end UI tests (offscreen Qt). The fake upscaler stands in for the GPU engine."""
import pytest

pytest.importorskip("PySide6.QtMultimedia")

from PySide6.QtGui import QDesktopServices  # noqa: E402
from PySide6.QtWidgets import QMessageBox  # noqa: E402

from framewise.core.settings import Settings  # noqa: E402
from framewise.media.probe import probe  # noqa: E402
from framewise.render.queue import JobStatus  # noqa: E402
from framewise.ui.inspector import Inspector  # noqa: E402
from framewise.ui.main_window import MainWindow  # noqa: E402

from .conftest import needs_ffmpeg  # noqa: E402


@pytest.fixture
def window(qtbot, tmp_path, toolset, monkeypatch):
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url) or True)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: pytest.fail(f"unexpected warning: {a[2]}"))
    # toolset fixture put the fake upscaler under tmp_path/engine; ffmpeg comes from PATH
    win = MainWindow(Settings(), tools_dir=tmp_path, check_tools_on_start=False)
    win.show()  # not registered with qtbot: we close it ourselves after marking the project saved
    win.opened_urls = opened
    yield win
    win.doc.undo.set_clean()  # skip the "save changes?" prompt
    for job in win.queue.jobs():
        win.queue.cancel(job.id)
    qtbot.waitUntil(lambda: not win.queue.busy, timeout=10000)
    assert win.close()
    win.deleteLater()


@needs_ffmpeg
def test_import_edit_undo_save_reopen(window, qtbot, sample_video, tmp_path):
    window.add_files([str(sample_video)])
    item = window.doc.project.items[0]
    qtbot.waitUntil(lambda: item.info is not None, timeout=10000)
    assert window.media.current_id() == item.id
    assert "64×48" in window.inspector.facts.text()
    assert window.inspector.lbl_size.text() == "128×96"

    window.inspector.cmb_target.setCurrentText("4x")
    assert item.edit.enhance.target == "4x" and window.inspector.lbl_size.text() == "256×192"
    assert window.windowTitle().startswith("Untitled*")
    window.act_undo.trigger()
    assert item.edit.enhance.target == "2x" and window.inspector.cmb_target.currentText() == "2x"
    window.act_redo.trigger()

    proj = tmp_path / "p.fwproj"
    assert window._save_to(proj)
    assert not window.windowTitle().startswith("Untitled") and "*" not in window.windowTitle()
    window.doc.new()
    assert window.media.list.count() == 0
    assert window.open_project(proj, confirmed=True)
    assert window.doc.project.items[0].edit.enhance.target == "4x"
    assert window.media.list.count() == 1


@needs_ffmpeg
def test_render_and_preview_through_queue(window, qtbot, sample_video):
    window.add_files([str(sample_video)])
    item = window.doc.project.items[0]
    qtbot.waitUntil(lambda: item.info is not None, timeout=10000)
    window.doc.update_edit(item.id, "enhance", preset="anime")

    preview = window.enqueue([item.id], preview=True)[0]
    full = window.enqueue([item.id])[0]
    qtbot.waitUntil(lambda: preview.status.finished and full.status.finished, timeout=60000)
    assert (preview.status, full.status) == (JobStatus.DONE, JobStatus.DONE), (preview.error, full.error)
    assert window.opened_urls, "preview should open automatically"
    info = probe(window.tools.ffprobe, full.spec.output)
    assert (info.width, info.height, info.frames) == (128, 96, 20)
    qtbot.waitUntil(lambda: window.queue_panel.tree.topLevelItemCount() == 2)
    window.queue.clear_finished()
    qtbot.waitUntil(lambda: window.queue_panel.tree.topLevelItemCount() == 0)


@needs_ffmpeg
def test_missing_file_is_flagged_not_rendered(window, qtbot, tmp_path, monkeypatch):
    ghost = tmp_path / "ghost.mp4"
    window.add_files([str(ghost)])
    item = window.doc.project.items[0]
    assert "missing" in window.inspector.warning.text().lower()
    assert not window.inspector.btn_queue.isEnabled()
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    assert window.enqueue([item.id]) == []
    assert warned and "ghost.mp4" in warned[0]


def test_inspector_quality_slider_commits_once_on_release(qtbot):
    from framewise.project.model import MediaItem
    insp = Inspector()
    qtbot.addWidget(insp)
    insp.set_item(MediaItem("/nonexistent.mp4"))
    seen = []
    insp.changed.connect(lambda i, section, ch: seen.append((section, ch)))
    insp.sld_quality.setSliderDown(True)
    for v in (20, 21, 22):
        insp.sld_quality.setValue(v)
    assert seen == []
    insp.sld_quality.setSliderDown(False)  # emits sliderReleased
    assert seen == [("export", {"quality": 22})]


# --- Step 2: ffmpeg feature detection is visible to the user -------------------------------------
@needs_ffmpeg
def test_capabilities_detected_in_background(window, qtbot):
    qtbot.waitUntil(lambda: window.capabilities is not None, timeout=10000)
    assert window.capabilities.available("crop") and window.capabilities.available("orient")


def test_setup_dialog_lists_feature_status(qtbot, tmp_path):
    from framewise.tools.capabilities import FEATURES, Capabilities
    from framewise.ui.setup_dialog import SetupDialog
    every = {f for feat in FEATURES.values() for g in feat.needs for f in g} | {"rubberband", "scale", "format"}
    caps = Capabilities(frozenset(every - {"transpose", "rubberband"}))
    dlg = SetupDialog(tmp_path, None, caps)
    qtbot.addWidget(dlg)
    dlg._refresh_features(ffmpeg_missing=True)
    assert "once ffmpeg is installed" in dlg.features.text()
    dlg._refresh_features(ffmpeg_missing=False)
    text = dlg.features.text()
    assert "✖ Rotate and flip: unavailable, ffmpeg lacks transpose" in text
    assert "◐ Speed change" in text and "0.1 s" in text
    assert "✔ Crop" in text and "✔ Trim" in text


@needs_ffmpeg
def test_render_with_missing_filter_fails_with_plain_message(window, qtbot, sample_video, monkeypatch):
    from framewise.tools.capabilities import Capabilities
    real = Capabilities.detect(window.tools.ffmpeg)
    monkeypatch.setattr(Capabilities, "detect", classmethod(lambda cls, ff: Capabilities(real.filters - {"transpose"})))
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a[2]))
    window.add_files([str(sample_video)])
    item = window.doc.project.items[0]
    window.doc.update_edit(item.id, "enhance", upscale=False)
    window.doc.update_edit(item.id, "transform", rotate=90)
    (job,) = window.enqueue([item.id])
    qtbot.waitUntil(lambda: job.status.finished, timeout=20000)
    assert job.status == JobStatus.FAILED
    assert "transpose" in job.error and "Rotate and flip" in job.error
    qtbot.waitUntil(lambda: bool(shown))
    assert "Rotate and flip" in shown[0]
    assert not job.spec.output.exists()
