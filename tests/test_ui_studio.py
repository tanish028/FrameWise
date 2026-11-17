"""Phase 2 UI workflows (offscreen Qt): studios, preview, history granularity, crop, black bars, trim,
presets and exports through the real render queue."""
import pytest

pytest.importorskip("PySide6.QtMultimedia")

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QDesktopServices  # noqa: E402
from PySide6.QtWidgets import QMessageBox  # noqa: E402

from framewise.core.settings import Settings  # noqa: E402
from framewise.media.probe import IMAGE, probe  # noqa: E402
from framewise.project.edit import CropRect  # noqa: E402
from framewise.render.export_presets import preset_settings  # noqa: E402
from framewise.render.queue import JobStatus  # noqa: E402
from framewise.ui.main_window import MainWindow  # noqa: E402
from framewise.ui.viewer import constrain_crop, ratio_value  # noqa: E402
from framewise.ui.widgets import format_time, parse_time  # noqa: E402

from .conftest import needs_ffmpeg  # noqa: E402
from .media_helpers import ff, make_index_video  # noqa: E402


@pytest.fixture
def win(qtbot, tmp_path, ff_tools, monkeypatch):
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: True)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    w = MainWindow(Settings(), tools_dir=tmp_path / "no-tools", check_tools_on_start=False)
    w.show()
    w.warnings = warnings
    yield w
    w.doc.undo.set_clean()
    for job in w.queue.jobs():
        w.queue.cancel(job.id)
    qtbot.waitUntil(lambda: not w.queue.busy, timeout=15000)
    assert w.close()
    w.deleteLater()


def make_png(path, size="64x48", pad=None):
    vf = ["-vf", pad] if pad else []
    ff("-f", "lavfi", "-i", f"testsrc2=s={size}:d=0.04", *vf, "-frames:v", "1", "-update", "1", path)
    return path


def wait_info(qtbot, item):
    qtbot.waitUntil(lambda: item.info is not None, timeout=10000)


# --- pure helpers ---------------------------------------------------------------------------------
def test_time_parsing_and_formatting():
    assert parse_time("83.5") == 83.5 and parse_time("1:23.5") == 83.5 and parse_time("0:01:23.500") == 83.5
    assert parse_time("abc") is None and parse_time("") is None
    assert format_time(83.5) == "1:23.500" and format_time(3725.25) == "1:02:05.250"


def test_crop_constraint_keeps_ratio_even_and_inside():
    c = constrain_crop(-10, 5, 101, 300, (640, 360), ratio_value("16:9"))
    assert c.x >= 0 and c.y >= 0 and c.x + c.w <= 640 and c.y + c.h <= 360
    assert all(v % 2 == 0 for v in (c.x, c.y, c.w, c.h))
    assert abs(c.w / c.h - 16 / 9) < 0.05
    free = constrain_crop(600, 300, 100, 100, (640, 360), None)
    assert free.x + free.w <= 640 and free.y + free.h <= 360


def test_platform_presets_follow_source_orientation():
    from framewise.media.probe import MediaInfo
    vertical = MediaInfo(1080, 1920, 30, "30/1", 300, 10, True, "h264", "aac", 1)
    ex, tr = preset_settings("whatsapp", vertical)
    assert (ex["width"], ex["height"]) == (720, 1280) and ex["rate_control"] == "size" and ex["target_mb"] < 16
    ex, tr = preset_settings("discord", None)
    assert ex["target_mb"] < 20 and (ex["width"], ex["height"]) == (1280, 720)
    ex, tr = preset_settings("shorts", None)
    assert (ex["width"], ex["height"]) == (1080, 1920) and tr == {"alignment": "blur"}
    ex, _ = preset_settings("gif", vertical)
    assert ex["format"] == "gif" and ex["width"] == 480 and ex["height"] % 2 == 0


# --- studios ----------------------------------------------------------------------------------------
@needs_ffmpeg
def test_importing_an_image_switches_to_image_studio_and_shows_image_sections(win, qtbot, tmp_path):
    vid = make_index_video(tmp_path / "v.mp4", frames=10)
    win.add_files([str(vid)])
    assert win.workspace == "video"
    win.add_files([str(make_png(tmp_path / "photo.png"))])
    img = win.doc.project.items[1]
    wait_info(qtbot, img)
    assert win.workspace == "image" and win.media.current_id() == img.id
    assert img.info.kind == IMAGE and img.edit.enhance.upscale is False, "AI upscaling is opt-in for images"
    insp = win.inspector
    assert insp.title.text() == "photo.png"
    assert not insp.sections["trim"].isVisibleTo(insp) and not insp.sections["speed"].isVisibleTo(insp)
    assert insp.sections["color"].isVisibleTo(insp) and insp.sections["ai"].isVisibleTo(insp)
    assert [insp.cmb_format.itemData(i) for i in range(insp.cmb_format.count())] == ["png", "jpeg", "webp", "bmp"]
    win._set_workspace("video")
    assert win.media.current_id() is None, "the image is not part of Video Studio's library"


@needs_ffmpeg
def test_preview_frame_appears_and_stale_results_are_dropped(win, qtbot, tmp_path):
    win.add_files([str(make_png(tmp_path / "p.png"))])
    item = win.doc.project.items[0]
    wait_info(qtbot, item)
    view = win.player.frame_view
    qtbot.waitUntil(view.has_frame, timeout=10000)
    assert view.edited_size == (64, 48)
    token = win._preview_token
    win.doc.update_edit(item.id, "transform", rotate=90)
    qtbot.waitUntil(lambda: win._preview_token > token and view.edited_size == (48, 64), timeout=10000)
    win.compare.buttons["side"].click()
    qtbot.waitUntil(lambda: view.original is not None, timeout=10000)


@needs_ffmpeg
def test_slider_drag_previews_without_history_and_commits_once(win, qtbot, tmp_path):
    win.add_files([str(make_png(tmp_path / "p.png"))])
    item = win.doc.project.items[0]
    wait_info(qtbot, item)
    row = win.inspector.color_rows["brightness"]
    before = win.doc.undo.undo_text()
    row.slider.setSliderDown(True)
    for v in (10, 20, 30, 40):
        row.slider.setValue(v)
    assert win._live_edit is not None and win._live_edit.color.brightness == 40
    assert item.edit.color.brightness == 0 and win.doc.undo.undo_text() == before, "dragging adds no history"
    row.slider.setSliderDown(False)  # release → one commit
    assert item.edit.color.brightness == 40
    win.act_undo.trigger()
    assert item.edit.color.brightness == 0 and win.doc.undo.undo_text() == before
    row.spin.setValue(-25)
    row.spin.editingFinished.emit()
    assert item.edit.color.brightness == -25
    row.reset.click()
    assert item.edit.color.brightness == 0


@needs_ffmpeg
def test_crop_tool_commits_drawn_rectangle_and_suggestion_needs_confirmation(win, qtbot, tmp_path):
    src = make_png(tmp_path / "bars.png", size="160x80", pad="pad=160:120:0:20:black")
    win.add_files([str(src)])
    item = win.doc.project.items[0]
    wait_info(qtbot, item)
    view = win.player.frame_view
    qtbot.waitUntil(view.has_frame, timeout=10000)
    win.inspector.btn_crop.setChecked(True)
    assert view.crop_mode
    view.crop_changed.emit(CropRect(10, 10, 100, 60))
    assert item.edit.transform.crop == CropRect(10, 10, 100, 60) and win.doc.undo.undo_text() == "Crop"
    win.doc.update_edit(item.id, "transform", crop=None)
    win.inspector.btn_bars.click()
    qtbot.waitUntil(lambda: item.id in win._suggested_crop, timeout=15000)
    suggestion = win._suggested_crop[item.id]
    assert abs(suggestion.y - 20) <= 2 and abs(suggestion.h - 80) <= 4
    assert item.edit.transform.crop is None, "a detected crop is never applied automatically"
    assert win.inspector.suggest_box.isVisibleTo(win.inspector)
    win.inspector.suggestion_accepted.emit(item.id)
    assert item.edit.transform.crop == suggestion and win.doc.undo.undo_text() == "Crop black bars"


@needs_ffmpeg
def test_trim_from_timeline_and_playhead(win, qtbot, tmp_path):
    vid = make_index_video(tmp_path / "v.mp4", rate="25", frames=50, audio="tone")
    win.add_files([str(vid)])
    item = win.doc.project.items[0]
    wait_info(qtbot, item)
    win.timeline.track.trim_changed.emit(0.4, 1.6)
    assert (item.edit.trim.start, item.edit.trim.end) == (0.4, 1.6)
    assert win.inspector.trim_start.text() == "0:00.400" and win.inspector.trim_end.text() == "0:01.600"
    win.inspector.trim_end.setText("0:01.200")
    win.inspector.trim_end.editingFinished.emit()
    assert item.edit.trim.end == 1.2
    win.timeline.track.trim_changed.emit(0.0, 2.0)
    assert item.edit.trim.end is None, "dragging the end handle to the end means 'to the end'"


@needs_ffmpeg
def test_platform_preset_is_one_undo_step(win, qtbot, tmp_path):
    vid = make_index_video(tmp_path / "v.mp4", frames=10)
    win.add_files([str(vid)])
    item = win.doc.project.items[0]
    wait_info(qtbot, item)
    insp = win.inspector
    insp.cmb_platform.setCurrentIndex(insp.cmb_platform.findData("shorts"))
    assert (item.edit.export.width, item.edit.export.height, item.edit.transform.alignment) == (1080, 1920, "blur")
    assert win.doc.undo.undo_text() == "Apply YouTube Shorts preset"
    win.act_undo.trigger()
    assert (item.edit.export.width, item.edit.transform.alignment) == (0, "fit")


@needs_ffmpeg
def test_image_and_audio_exports_through_the_queue(win, qtbot, tmp_path):
    win.add_files([str(make_png(tmp_path / "p.png"))])
    img = win.doc.project.items[0]
    wait_info(qtbot, img)
    win.doc.update_edit(img.id, "export", image_format="jpeg")
    win.doc.update_edit(img.id, "color", saturation=-100)
    (job,) = win.enqueue([img.id])
    qtbot.waitUntil(lambda: job.status.finished, timeout=20000)
    assert job.status == JobStatus.DONE, job.error
    assert job.spec.output.suffix == ".jpg" and probe(win.tools.ffprobe, job.spec.output).kind == IMAGE
    assert win.enqueue([img.id], preview=True) == [], "images have no 3-second preview render"

    vid = make_index_video(tmp_path / "v.mp4", frames=25, audio="tone")
    win.add_files([str(vid)])
    v = win.doc.project.items[1]
    wait_info(qtbot, v)
    win.doc.update_edit(v.id, "enhance", upscale=False)
    win.doc.update_edit(v.id, "export", audio_format="wav")
    (audio_job,) = win.enqueue([v.id], audio_only=True)
    qtbot.waitUntil(lambda: audio_job.status.finished, timeout=20000)
    assert audio_job.status == JobStatus.DONE and audio_job.spec.output.suffix == ".wav"
    assert not win.warnings


@needs_ffmpeg
def test_new_settings_keys_persist_and_old_files_still_load(tmp_path):
    from framewise.core.settings import Settings as S
    p = tmp_path / "s.json"
    p.write_text('{"default_quality": 20}')  # a Phase 1 settings file
    s = S.load(p)
    assert s.default_quality == 20 and s.preview_width == 1280 and s.workspace == "video"
    s.preview_ai, s.workspace = True, "image"
    s.save(p)
    assert S.load(p).preview_ai is True and S.load(p).workspace == "image"
