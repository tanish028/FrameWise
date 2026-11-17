"""Filter graph checked with real ffmpeg, at the pixel and sample level, on both render paths.

* Geometry: a red marker block must land exactly where rotating/flipping/cropping the source predicts,
  with and without AI upscaling, and for phone videos carrying display-rotation metadata.
* Timing: frames encode their own index, so trims and speed changes are verified frame by frame,
  and the chunked AI path must produce exactly the same frames as the single-pass path.
* Audio: a tone that starts at a known moment proves audio stays in sync after trim + speed,
  and zero-crossing counts prove pitch is preserved.
"""
import threading

import pytest

from framewise.core.errors import Cancelled
from framewise.media.probe import probe
from framewise.project.edit import CropRect, Edit, Enhance, Export, Speed, Transform, Trim
from framewise.render.graph import build_plan
from framewise.render.pipeline import run_render, work_dir_for
from framewise.render.spec import RenderSpec
from framewise.tools.capabilities import Capabilities

from .conftest import needs_ffmpeg
from .media_helpers import (MARK, audio_samples, dominant_frequency, frame_indices, make_index_video,
                            make_marker_video, onset_seconds, probe_streams, red_bbox, rgb_frames)

pytestmark = needs_ffmpeg


def spec(src, out, *, ai=False, preset="anime", seg=8, max_seconds=None, **sections):
    edit = Edit(enhance=Enhance(upscale=ai, preset=preset, target="2x"), export=Export(quality=10), **sections)
    return RenderSpec(src, out, edit.validated(), max_seconds=max_seconds, segment_frames=seg)


def expected_bbox(box, w, h, rot, fh, fv, crop=None, scale=1):
    """Where a rectangle (x0,y0,x1,y1) ends up after rotate (cw), flips, crop and scaling."""
    x0, y0, x1, y1 = box
    if rot == 90:
        x0, y0, x1, y1, w, h = h - y1, x0, h - y0, x1, h, w
    elif rot == 270:
        x0, y0, x1, y1, w, h = y0, w - x1, y1, w - x0, h, w
    elif rot == 180:
        x0, y0, x1, y1 = w - x1, h - y1, w - x0, h - y0
    if fh:
        x0, x1 = w - x1, w - x0
    if fv:
        y0, y1 = h - y1, h - y0
    if crop:
        x0, x1 = max(0, x0 - crop.x), min(crop.w, x1 - crop.x)
        y0, y1 = max(0, y0 - crop.y), min(crop.h, y1 - crop.y)
    return tuple(v * scale for v in (x0, y0, x1, y1))


def assert_box(actual, expected, tol=1):
    assert expected[0] < expected[2] and expected[1] < expected[3], f"test bug: marker cropped out {expected}"
    assert actual is not None, "marker not found"
    assert all(abs(a - e) <= tol for a, e in zip(actual, expected)), f"{actual} != {expected}"


MARK_BOX = (MARK[0], MARK[1], MARK[0] + MARK[2], MARK[1] + MARK[3])


# --- geometry -------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def marker(tmp_path_factory):
    return make_marker_video(tmp_path_factory.mktemp("m") / "marker.mp4")


@pytest.mark.parametrize("rot", [0, 90, 180, 270])
@pytest.mark.parametrize("fh,fv", [(False, False), (True, False), (False, True), (True, True)])
def test_orientation_moves_marker_exactly(marker, ff_tools, tmp_path, rot, fh, fv):
    out = run_render(spec(marker, tmp_path / "o.mp4", transform=Transform(rotate=rot, flip_h=fh, flip_v=fv)), ff_tools)
    w, h, frames = rgb_frames(out)
    assert (w, h) == ((48, 64) if rot in (90, 270) else (64, 48))
    assert_box(red_bbox(frames[0], w, h), expected_bbox(MARK_BOX, 64, 48, rot, fh, fv))


@pytest.mark.parametrize("rot,fh,crop", [
    (0, False, CropRect(0, 0, 32, 24)),
    (90, False, CropRect(24, 0, 24, 32)),     # crop in the rotated (48x64) frame, around the moved marker
    (270, True, CropRect(24, 44, 24, 20)),    # marker moves to x 40-48, y 48-64 before this crop
    (180, False, CropRect(40, 30, 24, 18)),
])
def test_crop_after_rotation_matches_drawn_rectangle_exactly(marker, ff_tools, tmp_path, rot, fh, crop):
    t = Transform(rotate=rot, flip_h=fh, crop=crop).validated()
    out = run_render(spec(marker, tmp_path / "o.mp4", transform=t), ff_tools)
    w, h, frames = rgb_frames(out)
    assert (w, h) == (t.crop.w, t.crop.h), "exported size must equal the crop rectangle"
    assert_box(red_bbox(frames[0], w, h), expected_bbox(MARK_BOX, 64, 48, rot, fh, False, t.crop))


@pytest.mark.parametrize("preset", ["anime", "general"])  # native 2x model / 4x model resized to 2x
def test_rotation_and_crop_happen_before_the_ai_upscaler(marker, toolset, tmp_path, preset):
    t = Transform(rotate=90, crop=CropRect(24, 0, 24, 32)).validated()
    out = run_render(spec(marker, tmp_path / "o.mp4", ai=True, preset=preset, transform=t), toolset)
    w, h, frames = rgb_frames(out)
    assert (w, h) == (48, 64), "upscaler must receive the cropped frame (24x32) and double it"
    assert_box(red_bbox(frames[0], w, h), expected_bbox(MARK_BOX, 64, 48, 90, False, False, t.crop, scale=2), tol=2)


def test_phone_video_rotation_metadata_is_respected_before_user_edits(ff_tools, tmp_path):
    toolset = ff_tools
    src = make_marker_video(tmp_path / "phone.mp4", display_rotation=90)
    info = probe(toolset.ffprobe, src)
    assert (info.width, info.height) == (48, 64), "probe must report the displayed (rotated) size"
    reference = run_render(spec(src, tmp_path / "ref.mp4"), toolset)  # what every player shows
    rw, rh, ref_frames = rgb_frames(reference)
    ref_box = red_bbox(ref_frames[0], rw, rh)
    crop = CropRect(0, 32, 48, 32)
    t = Transform(crop=crop).validated()
    if not (crop.y < ref_box[3] and ref_box[1] < crop.y + crop.h):
        crop = CropRect(0, 0, 48, 32)
        t = Transform(crop=crop).validated()
    out = run_render(spec(src, tmp_path / "o.mp4", transform=t), toolset)
    w, h, frames = rgb_frames(out)
    assert (w, h) == (crop.w, crop.h)
    assert_box(red_bbox(frames[0], w, h), expected_bbox(ref_box, rw, rh, 0, False, False, crop))


# --- trim & speed ----------------------------------------------------------------------------------
@pytest.mark.parametrize("rate_str,trim,expected", [
    ("25", Trim(start=0.4, end=1.2), list(range(10, 30))),
    ("30000/1001", Trim(start=0.5, end=1.0), list(range(15, 30))),   # round(0.5*29.97)=15, round(1.0*29.97)=30
    ("25", Trim(start=0.0, end=0.6), list(range(0, 15))),
    ("25", Trim(start=1.0), list(range(25, 40))),
])
@pytest.mark.parametrize("ai", [False, True])
def test_trim_is_frame_exact_on_both_paths(get_tools, tmp_path, rate_str, trim, expected, ai):
    toolset = get_tools(ai)
    src = make_index_video(tmp_path / "idx.mp4", rate=rate_str, frames=40)
    out = run_render(spec(src, tmp_path / "o.mp4", ai=ai, trim=trim.validated()), toolset)
    assert frame_indices(out) == expected


@pytest.mark.parametrize("rate", [0.25, 0.5, 0.7, 1.3, 2.0, 4.0])
def test_speed_single_pass_and_chunked_ai_produce_identical_frames(toolset, tmp_path, rate):
    src = make_index_video(tmp_path / "idx.mp4", rate="30000/1001", frames=40)
    sp = Speed(rate=rate).validated()
    single = frame_indices(run_render(spec(src, tmp_path / "a.mp4", speed=sp), toolset))
    chunked = frame_indices(run_render(spec(src, tmp_path / "b.mp4", ai=True, seg=7, speed=sp), toolset))
    plan = build_plan(Edit(speed=sp, enhance=Enhance(upscale=False)), probe(toolset.ffprobe, src))
    assert len(single) == plan.out_frames
    assert chunked == single, "chunk boundaries must not change which frames are shown"
    assert single == sorted(single) and single[0] < rate + 1 and single[-1] >= 39 - rate - 1


def test_trim_plus_speed_plus_preview_window(ff_tools, tmp_path):
    src = make_index_video(tmp_path / "idx.mp4", rate="25", frames=54)
    s = spec(src, tmp_path / "o.mp4", trim=Trim(start=0.8, end=2.2).validated(), speed=Speed(rate=2).validated(),
             max_seconds=1.0)
    idx = frame_indices(run_render(s, ff_tools))
    assert idx[0] == 20 and len(idx) == 13  # 25 source frames from frame 20, at 2x → round(25/2)
    assert all(b > a for a, b in zip(idx, idx[1:]))


# --- audio sync ------------------------------------------------------------------------------------
@pytest.fixture(params=["rubberband", "atempo"])
def stretcher(request, monkeypatch, ff_tools):
    """Run audio tests with rubberband, and again as if this ffmpeg lacked it (atempo fallback)."""
    real = Capabilities.detect(ff_tools.ffmpeg)
    if request.param == "rubberband" and not real.has("rubberband"):
        pytest.skip("this ffmpeg has no rubberband filter")
    if request.param == "atempo":
        limited = Capabilities(real.filters - {"rubberband"})
        monkeypatch.setattr(Capabilities, "detect", classmethod(lambda cls, ff: limited))
    return request.param


# onset tolerance per stretcher (seconds): measured worst cases are ~26 ms and ~116 ms at 0.25x
ONSET_TOL = {"rubberband": 0.03, "atempo": 0.12}


@pytest.mark.parametrize("ai", [False, True])
@pytest.mark.parametrize("start,rate", [(0.0, 1.0), (0.6, 1.0), (0.6, 2.0), (0.4, 0.5), (0.0, 0.75), (0.2, 0.25)])
def test_audio_stays_in_sync_after_trim_and_speed(get_tools, tmp_path, ai, start, rate, stretcher):
    toolset = get_tools(ai)
    src = make_index_video(tmp_path / "beep.mp4", rate="25", frames=50, audio="beep")  # tone starts at 1.0 s
    s = spec(src, tmp_path / "o.mp4", ai=ai, trim=Trim(start=start, end=1.8).validated(),
             speed=Speed(rate=rate).validated())
    out = run_render(s, toolset)
    streams = probe_streams(out)
    v_dur, a_dur = float(streams["video"]["duration"]), float(streams["audio"]["duration"])
    expected = (1.8 - start) / rate
    assert v_dur == pytest.approx(expected, abs=0.045), "speed/trim must never cut video frames"
    assert a_dur == pytest.approx(v_dur, abs=0.03), "audio and video must end together"
    onset = onset_seconds(audio_samples(out))
    tol = ONSET_TOL[stretcher] if rate != 1 else 0.01
    assert onset == pytest.approx((1.0 - start) / rate, abs=tol), "the tone must start at the re-timed moment"


@pytest.mark.parametrize("rate", [0.25, 0.5, 2.0, 4.0])
def test_speed_change_keeps_audio_pitch(ff_tools, tmp_path, rate, stretcher):
    toolset = ff_tools
    src = make_index_video(tmp_path / "tone.mp4", rate="25", frames=50, audio="tone")
    out = run_render(spec(src, tmp_path / "o.mp4", speed=Speed(rate=rate).validated()), toolset)
    assert dominant_frequency(audio_samples(out)) == pytest.approx(440, rel=0.03)


# --- resume ------------------------------------------------------------------------------------------
def test_resume_with_trim_and_speed_matches_uninterrupted_render(toolset, tmp_path):
    src = make_index_video(tmp_path / "idx.mp4", rate="25", frames=50, audio="tone")
    sections = dict(trim=Trim(start=0.2, end=1.9).validated(), speed=Speed(rate=0.7).validated(),
                    transform=Transform(rotate=90).validated())
    reference = frame_indices(run_render(spec(src, tmp_path / "ref.mp4", **sections), toolset))
    s = spec(src, tmp_path / "o.mp4", ai=True, seg=8, **sections)
    ev = threading.Event()
    with pytest.raises(Cancelled):
        run_render(s, toolset, ev, lambda p: p.stage == "Extracting" and p.detail.startswith("Chunk 3") and ev.set())
    finished = len(list(work_dir_for(s).glob("seg_*.mp4")))
    assert finished >= 2, "at least the first two chunks finished before the cancel"
    events = []
    out = run_render(s, toolset, None, events.append)
    assert sum(e.stage == "Resuming" for e in events) == finished, "every finished chunk is reused"
    assert frame_indices(out) == reference


def test_changed_speed_discards_old_chunks(toolset, tmp_path):
    src = make_index_video(tmp_path / "idx.mp4", rate="25", frames=40)
    s1 = spec(src, tmp_path / "o.mp4", ai=True, seg=8, speed=Speed(rate=0.5).validated())
    ev = threading.Event()
    with pytest.raises(Cancelled):
        run_render(s1, toolset, ev, lambda p: p.stage == "Extracting" and p.detail.startswith("Chunk 2") and ev.set())
    s2 = spec(src, tmp_path / "o.mp4", ai=True, seg=8, speed=Speed(rate=2.0).validated())
    events = []
    out = run_render(s2, toolset, None, events.append)
    assert not any(e.stage == "Resuming" for e in events), "chunks made at another speed must not be reused"
    assert frame_indices(out) == frame_indices(run_render(spec(src, tmp_path / "r.mp4", speed=s2.edit.speed), toolset))


# --- awkward Windows-style paths ------------------------------------------------------------------------
@pytest.mark.parametrize("ai", [False, True])
def test_spaces_quotes_unicode_and_multiple_dots_in_paths(get_tools, tmp_path, ai):
    toolset = get_tools(ai)
    folder = tmp_path / "Tanish's clips" / "Vidéo 日本 test"
    folder.mkdir(parents=True)
    src = make_index_video(folder / "my.clip.final.v2 — été.mp4", rate="25", frames=30, audio="tone")
    out = tmp_path / "out folder (1)" / "résultat.final.v3.mp4"
    s = spec(src, out, ai=ai, trim=Trim(start=0.2).validated(), speed=Speed(rate=1.5).validated(),
             transform=Transform(rotate=90, crop=CropRect(0, 0, 16, 16)).validated())
    assert run_render(s, toolset) == out
    streams = probe_streams(out)
    assert (int(streams["video"]["width"]), int(streams["video"]["height"])) == ((32, 32) if ai else (16, 16))
    assert int(streams["video"]["nb_read_frames"]) == build_plan(s.edit, probe(toolset.ffprobe, src)).out_frames
    assert "audio" in streams
    assert not work_dir_for(s).exists() and not any(out.parent.glob("*.part*"))
