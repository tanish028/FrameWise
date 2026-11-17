import threading
import time

import pytest

from framewise.core.errors import Cancelled, ToolError
from framewise.media.probe import probe
from framewise.media.thumbnails import make_thumbnail
from framewise.project.model import ClipSettings, MediaItem
from framewise.render.pipeline import Progress, run_render, work_dir_for
from framewise.render.queue import JobStatus, RenderQueue
from framewise.render.spec import RenderSpec, spec_from_item
from framewise.tools.encoders import detect_available

from .conftest import make_video, needs_ffmpeg


def _spec(src, out, **kw):
    base = dict(input=src, output=out, upscale=True, preset="anime", target="2x",
                encoder="libx264", quality=20, segment_frames=8)
    base.update(kw)
    return RenderSpec(**base)


# --- spec ----------------------------------------------------------------
def test_spec_from_item_names_and_preview(tmp_path):
    src = tmp_path / "v.mp4"
    src.write_bytes(b"x")
    item = MediaItem(str(src), ClipSettings(target="1080p"))
    assert spec_from_item(item).output.name == "v_1080p.mp4"
    (tmp_path / "v_1080p.mp4").write_bytes(b"x")
    assert spec_from_item(item).output.name == "v_1080p (2).mp4"  # never overwrite silently
    prev = spec_from_item(item, preview=True)
    assert prev.output.name == "v_1080p_preview.mp4" and prev.max_seconds == 3.0
    item.settings = ClipSettings(upscale=False)
    assert spec_from_item(item, output_dir=tmp_path / "out").output == tmp_path / "out" / "v_export.mp4"


# --- pipeline --------------------------------------------------------------
@needs_ffmpeg
def test_probe_and_thumbnail(sample_video, toolset, tmp_path):
    info = probe(toolset.ffprobe, sample_video)
    assert (info.width, info.height, info.frames, info.has_audio) == (64, 48, 20, True)
    thumb = make_thumbnail(toolset.ffmpeg, sample_video, tmp_path / "cache", info.duration, width=32)
    assert thumb.exists() and make_thumbnail(toolset.ffmpeg, sample_video, tmp_path / "cache", 1, 32) == thumb


@needs_ffmpeg
def test_upscale_chunked_keeps_audio_and_cleans_up(sample_video, toolset, tmp_path):
    events: list[Progress] = []
    out = run_render(_spec(sample_video, tmp_path / "o.mp4"), toolset, on_progress=events.append)
    info = probe(toolset.ffprobe, out)
    assert (info.width, info.height, info.frames, info.has_audio) == (128, 96, 20, True)
    assert events[-1].stage == "Done" and any(e.stage == "Upscaling" for e in events)
    assert not work_dir_for(_spec(sample_video, tmp_path / "o.mp4")).exists()


@needs_ffmpeg
def test_upscale_with_resize_for_4x_only_model(sample_video, toolset, tmp_path):
    out = run_render(_spec(sample_video, tmp_path / "o.mp4", preset="general"), toolset)
    info = probe(toolset.ffprobe, out)
    assert (info.width, info.height) == (128, 96)


@needs_ffmpeg
def test_plain_export_and_preview_length(sample_video, toolset, tmp_path):
    out = run_render(_spec(sample_video, tmp_path / "t.mp4", upscale=False), toolset)
    assert probe(toolset.ffprobe, out).frames == 20
    prev = run_render(_spec(sample_video, tmp_path / "p.mp4", max_seconds=0.5), toolset)
    assert probe(toolset.ffprobe, prev).frames == 10


@needs_ffmpeg
def test_video_without_audio(toolset, tmp_path):
    src = make_video(tmp_path / "silent.mp4", audio=False)
    for up in (True, False):
        out = run_render(_spec(src, tmp_path / f"o{up}.mp4", upscale=up), toolset)
        assert not probe(toolset.ffprobe, out).has_audio


@needs_ffmpeg
def test_cancel_then_resume_reuses_finished_chunks(sample_video, toolset, tmp_path):
    spec = _spec(sample_video, tmp_path / "o.mp4")
    ev = threading.Event()

    def stop_in_second_chunk(p):
        if p.stage == "Extracting" and p.detail.startswith("Chunk 2"):
            ev.set()

    with pytest.raises(Cancelled):
        run_render(spec, toolset, ev, stop_in_second_chunk)
    assert (work_dir_for(spec) / "seg_00000.mp4").exists()
    assert not spec.output.exists()
    events = []
    run_render(spec, toolset, None, events.append)
    assert any(e.stage == "Resuming" for e in events)
    assert probe(toolset.ffprobe, spec.output).frames == 20


@needs_ffmpeg
def test_render_errors(sample_video, toolset, tmp_path):
    with pytest.raises(ToolError):
        run_render(_spec(tmp_path / "missing.mp4", tmp_path / "o.mp4"), toolset)
    with pytest.raises(ToolError):
        run_render(_spec(sample_video, sample_video), toolset)
    with pytest.raises(ValueError):
        run_render(_spec(sample_video, tmp_path / "o.mp4", target="1x"), toolset)
    no_ai = type(toolset)(toolset.ffmpeg, toolset.ffprobe, None)
    with pytest.raises(ToolError, match="not installed"):
        run_render(_spec(sample_video, tmp_path / "o.mp4"), no_ai)


@needs_ffmpeg
def test_encoder_detection_always_includes_software(toolset):
    assert "libx264" in detect_available(toolset.ffmpeg)


# --- queue (fake runner, no ffmpeg needed) -----------------------------------
def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_queue_runs_in_order_and_reports(tmp_path):
    ran, gate = [], threading.Event()

    def runner(spec, tools, cancel, progress):
        gate.wait(5)
        progress(Progress("Upscaling", 1, 2))
        if spec.output.name == "bad.mp4":
            raise ToolError("engine exploded")
        ran.append(spec.output.name)
        return spec.output

    q = RenderQueue(lambda: object(), runner)
    a = q.add(_spec(tmp_path / "i", tmp_path / "a.mp4"), "a")
    assert _wait(lambda: a.status == JobStatus.RUNNING)
    b = q.add(_spec(tmp_path / "i", tmp_path / "bad.mp4"), "b")
    c = q.add(_spec(tmp_path / "i", tmp_path / "c.mp4"), "c")
    q.cancel(c.id)
    p = q.add(_spec(tmp_path / "i", tmp_path / "p.mp4"), "p", preview=True)
    gate.set()
    assert _wait(lambda: not q.busy)
    assert ran == ["a.mp4", "p.mp4"]  # preview jumped ahead of the still-queued job
    assert (a.status, b.status, c.status) == (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED)
    assert "exploded" in b.error
    q.retry(c.id)
    assert _wait(lambda: c.status == JobStatus.DONE)
    q.clear_finished()
    assert q.jobs() == []
    q.shutdown()


def test_queue_cancels_running_job_and_survives_crashes(tmp_path):
    started = threading.Event()

    def runner(spec, tools, cancel, progress):
        if spec.output.name == "crash.mp4":
            raise RuntimeError("bug")
        started.set()
        while not cancel.wait(0.02):
            pass
        raise Cancelled()

    q = RenderQueue(lambda: object(), runner)
    crash = q.add(_spec(tmp_path / "i", tmp_path / "crash.mp4"), "crash")
    job = q.add(_spec(tmp_path / "i", tmp_path / "x.mp4"), "x")
    assert started.wait(5)
    q.remove(job.id)  # cannot remove a running job
    assert q.get(job.id) is job
    q.cancel(job.id)
    assert _wait(lambda: job.status == JobStatus.CANCELLED)
    assert crash.status == JobStatus.FAILED and "Unexpected" in crash.error
    q.shutdown()


def test_queue_reports_missing_tools(tmp_path):
    q = RenderQueue(lambda: None, lambda *a: None)
    job = q.add(_spec(tmp_path / "i", tmp_path / "x.mp4"), "x")
    assert _wait(lambda: job.status == JobStatus.FAILED)
    assert "ffmpeg" in job.error
    q.shutdown()
