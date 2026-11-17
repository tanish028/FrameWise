"""Filter graph builder: ordering, geometry, timing maths and exact Phase 1 command parity (no ffmpeg run)."""
import importlib.util
import itertools
from fractions import Fraction
from pathlib import Path

import pytest

from framewise.media.probe import MediaInfo
from framewise.project.edit import CropRect, Edit, Enhance, Speed, Transform, Trim
from framewise.render import ffmpeg_ops
from framewise.render.graph import (Stage, atempo_chain, build_plan, compute_window, exact_rate,
                                    orientation_filters, oriented_size, round_half_up, stretch_filters,
                                    timing_filters)
from framewise.tools.capabilities import (FEATURES, Capabilities, MissingFeatureError, parse_filters)
from framewise.tools.encoders import encoder_args

FF = Path("ffmpeg")


def info(w=1920, h=1080, fps="30000/1001", frames=300, audio=True) -> MediaInfo:
    f = Fraction(fps)
    return MediaInfo(width=w, height=h, fps=float(f), fps_str=fps, frames=frames, duration=frames / float(f),
                     has_audio=audio, video_codec="h264", audio_codec="aac" if audio else None, size_bytes=1)


def no_ai(**sections) -> Edit:
    return Edit(enhance=Enhance(upscale=False), **sections)


# --- orientation -----------------------------------------------------------------------------
@pytest.mark.parametrize("rot,fh,fv,expected", [
    (0, False, False, []),
    (90, False, False, ["transpose=clock"]),
    (270, False, False, ["transpose=cclock"]),
    (180, False, False, ["hflip", "vflip"]),
    (180, True, False, ["vflip"]),           # 180° + mirror = vertical flip only
    (180, True, True, []),                   # 180° + both mirrors = no change at all
    (90, True, False, ["transpose=clock", "hflip"]),
    (0, False, True, ["vflip"]),
])
def test_orientation_filters(rot, fh, fv, expected):
    assert orientation_filters(rot, fh, fv) == expected


def test_oriented_size():
    assert oriented_size(1920, 1080, 90) == (1080, 1920) == oriented_size(1920, 1080, 270)
    assert oriented_size(1920, 1080, 180) == (1920, 1080)


# --- windows / trim ----------------------------------------------------------------------------
def test_window_default_and_preview_are_not_trims():
    w, trimmed = compute_window(Trim(), info(), None)
    assert (w.start_frame, w.frames, trimmed) == (0, 300, False)
    w, trimmed = compute_window(Trim(), info(), 3.0)
    assert (w.start_frame, w.frames, trimmed) == (0, 90, False)  # round(3 * 29.97)


def test_window_trim_is_frame_exact_at_ntsc_rate():
    w, trimmed = compute_window(Trim(start=2.0, end=6.0).validated(), info(), None)
    assert trimmed and w.start_frame == 60 and w.frames == 120  # round(2*29.97)=60, round(6*29.97)=180
    assert w.start == pytest.approx(60 * 1001 / 30000)
    w, _ = compute_window(Trim(start=2.0, end=6.0).validated(), info(), 1.0)
    assert (w.start_frame, w.frames) == (60, 30)
    w, _ = compute_window(Trim(start=1000).validated(), info(), None)
    assert w.start_frame < 300 and w.frames >= 1


# --- timing ------------------------------------------------------------------------------------
@pytest.mark.parametrize("rate", [0.25, 0.333, 0.5, 0.7, 0.75, 1.25, 1.5, 2.0, 3.7, 4.0])
def test_atempo_chain_multiplies_to_rate_within_bounds(rate):
    chain = atempo_chain(rate)
    product = 1.0
    for f in chain:
        v = float(f.split("=")[1])
        assert 0.5 <= v <= 2.0
        product *= v
    assert product == pytest.approx(rate, rel=1e-5)
    assert atempo_chain(1.0) == []


def test_timing_filters_use_integer_timestamps():
    assert timing_filters(Fraction(7, 10), Fraction(30000, 1001), 16) == [
        "settb=1/210000", "setpts=(16+N)*10010", "fps=30000/1001"]
    # one frame step = 10010 ticks of 1/210000 s == exactly 1 / (29.97 fps * 0.7)
    assert Fraction(10010, 210000) == 1 / (Fraction(30000, 1001) * Fraction(7, 10))


@pytest.mark.parametrize("rate,chunk", list(itertools.product([0.25, 0.333, 0.7, 1.3, 2.0, 3.7, 4.0], [1, 7, 8, 240])))
def test_chunk_frame_counts_add_up_exactly(rate, chunk):
    plan = build_plan(no_ai(speed=Speed(rate=rate)), info(frames=97), max_seconds=None)
    total = sum(plan.chunk_out_frames(s, min(chunk, 97 - s)) for s in range(0, 97, chunk))
    exact = Fraction(97) / exact_rate(rate)
    assert total == plan.out_frames == int(exact + Fraction(1, 2))


def test_half_frames_round_up_like_ffmpeg():
    assert [round_half_up(Fraction(n, 2)) for n in (1, 3, 5, 21)] == [1, 2, 3, 11]
    assert round(Fraction(21, 2)) == 10, "Python rounds half to even: must not be used for frame maths"
    plan = build_plan(no_ai(speed=Speed(rate=2)), info(frames=25))
    assert plan.out_frames == 13


def test_stretchers():
    assert stretch_filters(Fraction(1), "rubberband") == []
    assert stretch_filters(Fraction(1, 2), "rubberband") == ["rubberband=tempo=0.5:transients=crisp:window=short"]
    assert stretch_filters(Fraction(1, 4), "atempo") == ["atempo=0.5", "atempo=0.5"]
    with pytest.raises(ValueError):
        stretch_filters(Fraction(2), "sox")


# --- ordering & geometry -------------------------------------------------------------------------
def test_full_ordering_around_ai_boundary():
    edit = Edit(trim=Trim(start=1, end=5), transform=Transform(rotate=90, crop=CropRect(10, 20, 600, 800)),
                enhance=Enhance(upscale=True, preset="general", target="2x"), speed=Speed(rate=0.5)).validated()
    plan = build_plan(edit, info())
    stages = [n.stage for n in plan.nodes]
    assert stages == sorted(stages)
    assert stages == [Stage.TRIM_RESET, Stage.ORIENT, Stage.CROP, Stage.RESIZE, Stage.TIME, Stage.FPS]
    assert plan.video_filters(before_ai=True) == ["setpts=PTS-STARTPTS", "transpose=clock", "crop=600:800:10:20"]
    assert plan.video_filters(before_ai=True, timestamp_reset=False) == ["transpose=clock", "crop=600:800:10:20"]
    assert plan.video_filters(before_ai=False) == ["scale=1200:1600:flags=lanczos"]  # 4x model → resized to 2x
    assert plan.timing_filters(0)[-1] == "fps=30000/1001"
    dur = f"{float(Fraction(plan.out_frames) / plan.window.fps):.6f}"
    assert plan.audio_filters == ("asetpts=PTS-STARTPTS", "apad=pad_dur=0.5",
                                  "rubberband=tempo=0.5:transients=crisp:window=short", f"atrim=end={dur}")
    atempo = build_plan(edit, info(), stretcher="atempo").audio_filters
    assert atempo[2] == "atempo=0.5" and atempo[-1] == f"atrim=end={dur}"
    # crop is in the rotated (1080x1920) frame and the upscaler sees the cropped size
    assert plan.oriented_size == (1080, 1920) and plan.base_size == (600, 800)
    assert plan.upscale.engine_scale == 4 and plan.out_size == (1200, 1600)


@pytest.mark.parametrize("stretcher", ["rubberband", "atempo"])
@pytest.mark.parametrize("rate", [0.25, 1.0, 2.0, 4.0])
def test_audio_padding_always_comes_before_the_stretch(stretcher, rate):
    """Padding after atempo deadlocks ffmpeg 8 at fast speeds; the final step must be the exact cut."""
    af = build_plan(no_ai(trim=Trim(start=1).validated(), speed=Speed(rate=rate)), info(),
                    stretcher=stretcher).audio_filters
    names = [f.split("=")[0] for f in af]
    assert names[0] == "asetpts" and names[1] == "apad" and names[-1] == "atrim"
    assert names.count("apad") == 1
    assert all(n in ("rubberband", "atempo") for n in names[2:-1])


def test_crop_is_clamped_to_rotated_frame_and_full_frame_crop_is_dropped():
    plan = build_plan(no_ai(transform=Transform(rotate=270, crop=CropRect(1000, 1800, 400, 400)).validated()), info())
    assert plan.crop.x + plan.crop.w <= 1080 and plan.crop.y + plan.crop.h <= 1920
    full = build_plan(no_ai(transform=Transform(crop=CropRect(0, 0, 1920, 1080)).validated()), info())
    assert full.crop is None and full.nodes == ()


def test_default_edit_needs_only_phase1_filters():
    plan = build_plan(Edit(), info())  # 4x "general" model resized to 2x, exactly as Phase 1 did
    assert [n.filters for n in plan.nodes] == [("scale=3840:2160:flags=lanczos",)]
    assert plan.audio_filters == () and plan.requires == frozenset({"scale"})
    assert plan.out_size == (3840, 2160)
    assert build_plan(Edit(enhance=Enhance(preset="anime")), info()).nodes == ()  # native 2x: no filters
    plan = build_plan(no_ai(), info())
    assert plan.requires == frozenset() and plan.out_size == (1920, 1080)


def test_requires_lists_every_filter_used():
    edit = no_ai(trim=Trim(start=1), transform=Transform(rotate=90, flip_h=True, crop=CropRect(0, 0, 100, 100)),
                 speed=Speed(rate=2)).validated()
    req = build_plan(edit, info()).requires
    assert {"setpts", "transpose", "hflip", "crop", "settb", "fps", "tpad", "asetpts", "rubberband",
            "apad", "atrim"} <= req
    assert "atempo" in build_plan(edit, info(), stretcher="atempo").requires


def test_invalid_upscale_raises_instead_of_guessing():
    with pytest.raises(ValueError):
        build_plan(Edit(enhance=Enhance(target="1x")), info())
    with pytest.raises(ValueError):
        build_plan(Edit(enhance=Enhance(preset="nope")), info())


# --- exact Phase 1 parity for default edits --------------------------------------------------------
def _phase1_module():
    path = Path(__file__).parent / "data" / "phase1" / "ffmpeg_ops_phase1.py"
    spec = importlib.util.spec_from_file_location("ffmpeg_ops_phase1", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def phase1(monkeypatch):
    mod = _phase1_module()
    calls = []
    monkeypatch.setattr(mod, "run_cancellable", lambda cmd, **kw: calls.append(list(cmd)) or "")
    monkeypatch.setattr(mod.os, "replace", lambda *a: None)
    return mod, calls


@pytest.mark.parametrize("has_audio", [True, False])
@pytest.mark.parametrize("max_seconds", [None, 3.0])
def test_transcode_command_identical_to_phase1(phase1, tmp_path, has_audio, max_seconds):
    mod, calls = phase1
    src, out = Path("C:/My Videos/a.b.mp4"), tmp_path / "o.mp4"
    mod.transcode(FF, src, out, encoder="libx265", quality=21, has_audio=has_audio,
                  max_seconds=max_seconds, work_dir=tmp_path)
    plan = build_plan(no_ai(), info(audio=has_audio), max_seconds=max_seconds)
    new = ffmpeg_ops.transcode_args(FF, src, ffmpeg_ops.part_path(out), tmp_path / "progress.txt", plan,
                                    encoder="libx265", quality=21, has_audio=has_audio, max_seconds=max_seconds)
    assert new == calls[0]


@pytest.mark.parametrize("start", [0, 240, 480])
def test_extract_command_identical_to_phase1(phase1, tmp_path, start):
    mod, calls = phase1
    plan = build_plan(Edit(), info())
    mod.extract_frames(FF, Path("in put.mp4"), tmp_path, start, 240, plan.window.fps.__float__())
    assert ffmpeg_ops.extract_args(FF, Path("in put.mp4"), tmp_path, plan, start, 240) == calls[0]


@pytest.mark.parametrize("preset", ["general", "anime"])  # 4x model resized to 2x / native 2x
def test_encode_command_identical_to_phase1(phase1, tmp_path, preset):
    mod, calls = phase1
    plan = build_plan(Edit(enhance=Enhance(preset=preset)), info())
    resize = plan.out_size if plan.upscale.needs_resize else None
    mod.encode_frames(FF, tmp_path, tmp_path / "seg.mp4", plan.fps_str, resize_to=resize,
                      encoder="libx264", quality=18)
    new = ffmpeg_ops.encode_chunk_args(FF, tmp_path, ffmpeg_ops.part_path(tmp_path / "seg.mp4"), plan, 0, 240,
                                       encoder="libx264", quality=18)
    assert new == calls[0]


@pytest.mark.parametrize("with_audio", [True, False])
@pytest.mark.parametrize("max_seconds", [None, 3.0])
def test_join_command_identical_to_phase1(phase1, tmp_path, with_audio, max_seconds):
    mod, calls = phase1
    segs = [tmp_path / "seg_00000.mp4"]
    plan = build_plan(Edit(), info(), max_seconds=max_seconds)
    mod.join_segments(FF, segs, Path("src.mp4"), tmp_path / "out.mp4", with_audio=with_audio,
                      max_seconds=max_seconds, work_dir=tmp_path)
    new = ffmpeg_ops.join_args(FF, tmp_path / "segments.txt", Path("src.mp4"),
                               ffmpeg_ops.part_path(tmp_path / "out.mp4"), plan,
                               with_audio=with_audio, max_seconds=max_seconds)
    assert new == calls[0]
    assert (tmp_path / "segments.txt").read_text() == ffmpeg_ops.concat_list_text(segs)


def _capture_runs(monkeypatch):
    calls = []
    monkeypatch.setattr(ffmpeg_ops, "run_cancellable", lambda cmd, **kw: calls.append([str(c) for c in cmd]) or "")
    monkeypatch.setattr(ffmpeg_ops.os, "replace", lambda *a: None)
    return calls


def test_default_export_is_one_pass_and_filtered_audio_gets_its_own_pass(monkeypatch, tmp_path):
    calls = _capture_runs(monkeypatch)
    kw = dict(encoder="libx264", quality=18, has_audio=True, max_seconds=None, work_dir=tmp_path)
    ffmpeg_ops.transcode(FF, Path("a.mp4"), tmp_path / "o.mp4", build_plan(no_ai(), info()), **kw)
    assert len(calls) == 1 and "-map" in calls[0] and "0:a:0" in calls[0]
    calls.clear()
    plan = build_plan(no_ai(speed=Speed(rate=2)), info())
    ffmpeg_ops.transcode(FF, Path("a.mp4"), tmp_path / "o.mp4", plan, **kw)
    video, mux = calls
    assert "-af" not in video and "0:a:0" not in video and "1:a:0" not in video, "video pass carries no audio"
    assert mux[mux.index("-c:v") + 1] == "copy" and "-af" in mux and "-progress" in mux
    calls.clear()
    ffmpeg_ops.transcode(FF, Path("a.mp4"), tmp_path / "o.mp4", plan, **{**kw, "has_audio": False})
    assert len(calls) == 1


def test_concat_list_escapes_quotes(tmp_path):
    text = ffmpeg_ops.concat_list_text([tmp_path / "Tanish's clips" / "a.mp4"])
    assert text.startswith("file '") and text.endswith("/Tanish'\\''s clips/a.mp4'\n")
    assert "\\" not in text.replace("'\\''", ""), "Windows backslashes must be written as forward slashes"


def test_encoder_args_unchanged():
    assert encoder_args("libx264", 18) == ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]


# --- capability detection -------------------------------------------------------------------------
SAMPLE_FILTERS = """Filters:
  T.. = Timeline support
  A = Audio input/output
  | = Source or sink filter
 ..C crop              V->V       Crop the input video.
 T.C cropdetect        V->V       Auto-detect crop size.
 ... split             V->N       Pass on the input to N video outputs.
 ... nullsrc           |->V       Null video source, return unprocessed video frames.
 ..C atempo            A->A       Adjust audio tempo.
"""


def test_parse_filters():
    assert parse_filters(SAMPLE_FILTERS) == {"crop", "cropdetect", "split", "nullsrc", "atempo"}


def test_feature_status_and_plain_language_error():
    caps = Capabilities(frozenset({"crop", "atempo", "setpts"}))
    status = {s.feature.key: s for s in caps.feature_status()}
    assert status["crop"].available and "transpose" in status["orient"].missing
    assert status["orient"].describe().startswith("✖ Rotate and flip: unavailable, ffmpeg lacks transpose")
    assert not caps.available("speed") and caps.available("crop")
    assert {p.feature.key for p in caps.problems()} >= {"orient", "speed"}
    with pytest.raises(MissingFeatureError) as e:
        caps.require({"transpose", "crop"})
    msg = str(e.value)
    assert "transpose" in msg and FEATURES["orient"].label in msg and "Download tools" in msg
    caps.require({"crop"})  # no error


def test_speed_without_rubberband_is_degraded_not_missing():
    full = {f for feat in FEATURES.values() for g in feat.needs for f in g}
    caps = Capabilities(frozenset(full - {"rubberband"}))
    speed = next(s for s in caps.feature_status() if s.feature.key == "speed")
    assert speed.available and speed.degraded and speed.describe().startswith("◐")
    assert [p.feature.key for p in caps.problems()] == ["speed"]
    assert Capabilities(frozenset(full)).problems() == []


def test_every_implemented_filter_is_covered_by_a_feature():
    edit = no_ai(trim=Trim(start=1), transform=Transform(rotate=90, flip_h=True, flip_v=True,
                                                       crop=CropRect(0, 0, 100, 100)),
                 speed=Speed(rate=2)).validated()
    declared = {f for feat in FEATURES.values() for group in feat.needs for f in group}
    for stretcher in ("rubberband", "atempo"):
        plan = build_plan(edit, info(), stretcher=stretcher)
        assert plan.requires <= declared, plan.requires - declared


# --- chunk layout (AI path) ------------------------------------------------------------------------
@pytest.mark.parametrize("total,seg,rate", [(50, 16, 1.0), (482, 240, 1.0), (50, 16, 2.0), (50, 12, 4.0),
                                             (43, 8, 0.7), (240, 240, 1.0), (5, 240, 1.0), (241, 240, 0.25)])
def test_chunk_ranges_cover_everything_and_never_end_with_a_tiny_chunk(total, seg, rate):
    from framewise.render.pipeline import MIN_TAIL_OUT_FRAMES, chunk_ranges
    plan = build_plan(no_ai(speed=Speed(rate=rate)), info(frames=total))
    ranges = chunk_ranges(total, seg, plan)
    assert ranges[0][0] == 0 and sum(n for _, n in ranges) == total
    assert all(a + n == b for (a, n), (b, _) in zip(ranges, ranges[1:])), "chunks must be contiguous"
    assert all(n <= seg for _, n in ranges[:-1])
    if len(ranges) > 1:
        assert plan.chunk_out_frames(*ranges[-1]) >= MIN_TAIL_OUT_FRAMES
    assert sum(plan.chunk_out_frames(s, n) for s, n in ranges) == plan.out_frames


def test_chunk_layout_unchanged_when_no_tiny_tail():
    from framewise.render.pipeline import chunk_ranges
    plan = build_plan(Edit(), info(frames=720))
    assert chunk_ranges(720, 240, plan) == [(0, 240), (240, 240), (480, 240)]
    assert chunk_ranges(482, 240, build_plan(Edit(), info(frames=482))) == [(0, 240), (240, 242)]
