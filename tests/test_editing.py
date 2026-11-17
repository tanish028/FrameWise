"""Phase 2 editing engine with real ffmpeg: colour, looks, canvas, enhance, images, audio, export formats,
reverse, stabilization, AI final encode, preview engine and black-bar detection."""
import hashlib
import os
import shutil
import threading

import pytest

from framewise.core.errors import FrameWiseError, ToolError
from framewise.media.probe import AUDIO, GIF, IMAGE, SEQUENCE, VIDEO, probe, probe_source
from framewise.media.source import Source, detect_sequence
from framewise.project.edit import (COLOR_SPECS, Audio, Color, CropRect, Edit, Enhance, Export, Filter, Speed,
                                    Transform, Trim)
from framewise.project.model import MediaItem
from framewise.render import ops
from framewise.render.analysis import detect_black_bars
from framewise.render.graph import build_plan
from framewise.render.pipeline import run_render
from framewise.render.preview import PreviewRequest, render_preview
from framewise.render.spec import AUDIO_JOB, IMAGE_JOB, RenderSpec, spec_from_item
from framewise.tools.capabilities import Capabilities

from .conftest import needs_ffmpeg
from .media_helpers import (MARK, audio_samples, ff, frame_indices, make_index_video, make_marker_video,
                            onset_seconds, probe_streams, red_bbox, rgb_frames)

pytestmark = needs_ffmpeg


def make_image(path, color="gray", size="64x48", box=True):
    src = f"color={color}:s={size}:d=0.04"
    if box:
        src += ",drawbox=x=0:y=0:w=16:h=8:color=red:t=fill"
    ff("-f", "lavfi", "-i", src, "-frames:v", "1", "-update", "1", path)
    return path


def rgb_mean(path):
    w, h, frames = rgb_frames(path)
    f = frames[0]
    n = w * h
    return tuple(sum(f[c::3]) / n for c in range(3))


def image_spec(src, out, edit=None, **export):
    e = (edit or Edit()).with_section("enhance", upscale=False) if not export.pop("ai", False) else (edit or Edit())
    if export:
        e = e.with_section("export", **export)
    return RenderSpec(src, out, e.validated(), kind=IMAGE_JOB)


def sha(path):
    return hashlib.sha1(path.read_bytes()).hexdigest()


# --- colour maths --------------------------------------------------------------------------------------
def test_neutral_colour_adds_no_filters():
    assert ops.color_filters(Color())[0] == [] and ops.vignette_filters(Color())[0] == []
    assert ops.apply_look(Color(), Filter("none", 100)) == (Color(), [])
    assert ops.apply_look(Color(), Filter("warm", 0)) == (Color(), [])


@pytest.mark.parametrize("field", ["highlights", "shadows", "whites", "blacks", "fade"])
@pytest.mark.parametrize("value", [-100, -40, 40, 100])
def test_tone_curve_is_always_monotonic(field, value):
    if field == "fade" and value < 0:
        return
    pts = ops.curve_points(Color(**{field: value}).validated())
    assert pts[0][0] == 0.0 and pts[-1][0] == 1.0
    assert all(b[0] > a[0] and b[1] >= a[1] for a, b in zip(pts, pts[1:]))


def test_look_strength_scales_linearly():
    full, _ = ops.apply_look(Color(), Filter("warm", 100))
    half, _ = ops.apply_look(Color(), Filter("warm", 50))
    assert half.temperature == pytest.approx(full.temperature / 2, abs=0.5)  # sliders snap to whole steps
    combined, _ = ops.apply_look(Color(temperature=-10), Filter("warm", 100))
    assert combined.temperature == pytest.approx(-10 + full.temperature)


@pytest.mark.parametrize("changes,check", [
    ({"brightness": 60}, lambda base, out: sum(out) > sum(base) + 20),
    ({"brightness": -60}, lambda base, out: sum(out) < sum(base) - 20),
    ({"exposure": 1.0}, lambda base, out: sum(out) > sum(base) + 20),
    ({"temperature": 80}, lambda base, out: out[0] - out[2] > base[0] - base[2] + 8),    # warmer = more red than blue
    ({"temperature": -80}, lambda base, out: out[2] - out[0] > base[2] - base[0] + 8),
    ({"tint": 80}, lambda base, out: out[1] < base[1] - 4),                              # magenta = less green
    ({"tint": -80}, lambda base, out: out[1] > base[1] + 4),
    ({"shadows": 80}, lambda base, out: sum(out) > sum(base) + 6),
    ({"fade": 80}, lambda base, out: sum(out) > sum(base)),
])
def test_colour_controls_move_pixels_the_right_way(ff_tools, tmp_path, changes, check):
    src = make_image(tmp_path / "gray.png", color="0x606060", box=False)
    base = rgb_mean(run_render(image_spec(src, tmp_path / "base.png"), ff_tools))
    out = rgb_mean(run_render(image_spec(src, tmp_path / "o.png", Edit().with_section("color", **changes)), ff_tools))
    assert check(base, out), (changes, base, out)


def test_saturation_minus_100_is_grey(ff_tools, tmp_path):
    src = make_image(tmp_path / "c.png", color="0xc03030", box=False)
    r, g, b = rgb_mean(run_render(image_spec(src, tmp_path / "o.png", Edit().with_section("color", saturation=-100)),
                                  ff_tools))
    assert max(r, g, b) - min(r, g, b) < 6


def test_every_slider_extreme_and_every_look_renders(ff_tools, tmp_path):
    src = make_image(tmp_path / "s.png")
    edit = Edit()
    for spec in COLOR_SPECS:
        edit = edit.with_section("color", **{spec.name: spec.hi})
    run_render(image_spec(src, tmp_path / "hi.png", edit), ff_tools)
    edit = Edit()
    for spec in COLOR_SPECS:
        edit = edit.with_section("color", **{spec.name: spec.lo})
    run_render(image_spec(src, tmp_path / "lo.png", edit), ff_tools)
    for look in ops.LOOKS:
        run_render(image_spec(src, tmp_path / f"{look}.png", Edit().with_section("filter", preset=look, strength=70)),
                   ff_tools)


@pytest.mark.parametrize("changes", [dict(sharpen=100), dict(blur=60), dict(denoise="fast", denoise_strength=80),
                                     dict(denoise="quality", denoise_strength=30)])
def test_enhance_filters_render_on_images(ff_tools, tmp_path, changes):
    src = make_image(tmp_path / "s.png")
    out = run_render(image_spec(src, tmp_path / "o.png", Edit().with_section("enhance", **changes)), ff_tools)
    assert rgb_frames(out)[:2] == (64, 48)


# --- canvas / alignment ---------------------------------------------------------------------------------
@pytest.mark.parametrize("alignment", ["fit", "fill", "blur", "stretch"])
def test_canvas_alignment_never_distorts_unless_stretch(ff_tools, tmp_path, alignment):
    src = make_image(tmp_path / "wide.png", color="white", size="128x48", box=False)
    edit = Edit().with_section("transform", alignment=alignment)
    out = run_render(image_spec(src, tmp_path / "o.png", edit, width=64, height=64), ff_tools)
    w, h, frames = rgb_frames(out)
    assert (w, h) == (64, 64)
    f = frames[0]
    top = sum(f[(2 * w + 32) * 3:(2 * w + 32) * 3 + 3]) / 3      # pixel near the top edge, centre
    centre = sum(f[(32 * w + 32) * 3:(32 * w + 32) * 3 + 3]) / 3
    assert centre > 200
    if alignment == "fit":
        assert top < 40, "fit letterboxes a wide picture into a square canvas with black bars"
    else:
        assert top > 150, "fill, blur fill and stretch cover the whole canvas"


def test_fill_crops_instead_of_squashing(ff_tools, tmp_path):
    src = make_marker_video(tmp_path / "m.mp4")  # 64x48, marker 16x8 at the top-left
    s = RenderSpec(src, tmp_path / "o.mp4", Edit(enhance=Enhance(upscale=False), transform=Transform(alignment="fill"),
                                                 export=Export(width=48, height=48)).validated())
    w, h, frames = rgb_frames(run_render(s, ff_tools))
    x0, y0, x1, y1 = red_bbox(frames[0], w, h)
    # 64x48 → 48x48 fill: scale stays 1:1 and 8 px are cut from each side, so the 16x8 marker becomes 8x8
    # (a squash would make it 12x8)
    assert (w, h) == (48, 48) and abs((x1 - x0) - 8) <= 1 and abs((y1 - y0) - 8) <= 1 and x0 == 0


# --- images ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt,ext", [("png", ".png"), ("jpeg", ".jpg"), ("webp", ".webp"), ("bmp", ".bmp")])
def test_image_export_formats_never_touch_the_original(ff_tools, tmp_path, fmt, ext):
    if fmt == "webp" and "libwebp" not in __import__("framewise.tools.encoders", fromlist=["x"]).compiled_encoders(
            ff_tools.ffmpeg):
        pytest.skip("ffmpeg built without libwebp")
    src = make_image(tmp_path / "in put.final.v2.png")
    before = sha(src)
    item = MediaItem(str(src), Edit().with_section("enhance", upscale=False).with_section("export", image_format=fmt)
                     .with_section("transform", rotate=90))
    item.info = probe(ff_tools.ffprobe, src)
    spec = spec_from_item(item)
    assert spec.kind == IMAGE_JOB and spec.output.suffix == ext
    out = run_render(spec, ff_tools)
    assert sha(src) == before
    assert probe(ff_tools.ffprobe, out).kind == IMAGE
    assert rgb_frames(out)[:2] == (48, 64)


def test_neutral_png_export_is_pixel_identical(ff_tools, tmp_path):
    src = make_image(tmp_path / "a.png")
    out = run_render(image_spec(src, tmp_path / "b.png"), ff_tools)
    assert rgb_frames(out)[2][0] == rgb_frames(src)[2][0]


def test_image_crop_resize_and_ai_upscale(toolset, tmp_path):
    src = make_image(tmp_path / "a.png")
    edit = Edit(transform=Transform(crop=CropRect(0, 0, 32, 24)), enhance=Enhance(upscale=True, preset="general",
                                                                                   target="2x")).validated()
    out = run_render(image_spec(src, tmp_path / "up.png", edit, ai=True), toolset)
    w, h, frames = rgb_frames(out)
    assert (w, h) == (64, 48)
    x0, y0, x1, y1 = red_bbox(frames[0], w, h)
    assert abs(x1 - 32) <= 2 and abs(y1 - 16) <= 2, "crop happens before the 2x upscale"


# --- media kinds ------------------------------------------------------------------------------------------
def test_media_kind_detection(ff_tools, tmp_path):
    img = make_image(tmp_path / "i.png")
    vid = make_index_video(tmp_path / "v.mp4", frames=10, audio="tone")
    wav = tmp_path / "a.wav"
    ff("-f", "lavfi", "-i", "sine=d=1", wav)
    gif = tmp_path / "g.gif"
    ff("-f", "lavfi", "-i", "testsrc=s=32x24:r=10:d=1", gif)
    kinds = {p.suffix: probe(ff_tools.ffprobe, p) for p in (img, vid, wav, gif)}
    assert kinds[".png"].kind == IMAGE and kinds[".png"].frames == 1 and kinds[".png"].is_still
    assert kinds[".mp4"].kind == VIDEO and kinds[".mp4"].has_audio
    assert kinds[".wav"].kind == AUDIO and not kinds[".wav"].has_video and kinds[".wav"].duration == pytest.approx(1, abs=.05)
    assert kinds[".gif"].kind == GIF and kinds[".gif"].frames == 10
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"not a video at all")
    with pytest.raises(ToolError):
        probe(ff_tools.ffprobe, bad)


def test_image_sequence_detect_probe_and_render(ff_tools, tmp_path):
    folder = tmp_path / "shots 100% final"   # a literal % must be escaped in the image2 pattern
    folder.mkdir()
    for i in range(1, 13):
        ff("-f", "lavfi", "-i", f"color=0x{i * 16:02x}{i * 16:02x}{i * 16:02x}:s=32x16:d=0.04", "-frames:v", "1",
           "-update", "1", folder / f"frame_{i:04d}.png")
    seq = detect_sequence(folder / "frame_0001.png", fps="12/1")
    assert seq is not None and seq.count == 12 and seq.start == 1
    assert seq.frame_path(7) == folder / "frame_0007.png"
    assert detect_sequence(folder / "frame_0012.png") is None, "a run of one frame is just an image"
    info = probe_source(ff_tools.ffprobe, folder / "frame_0001.png", seq)
    assert info.kind == SEQUENCE and info.frames == 12 and info.duration == pytest.approx(1.0)
    item = MediaItem(str(folder / "frame_0001.png"), Edit().with_section("enhance", upscale=False), sequence=seq)
    out = run_render(spec_from_item(item, output_dir=tmp_path), ff_tools)
    assert int(probe_streams(out)["video"]["nb_read_frames"]) == 12


# --- audio --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt", ["mp3", "wav", "m4a", "flac"])
def test_audio_file_export_with_trim_speed_volume_and_fades(ff_tools, tmp_path, fmt):
    wav = tmp_path / "beep.wav"
    ff("-f", "lavfi", "-i", "aevalsrc='if(gte(t,1.0),0.5*sin(2*PI*440*t),0)':s=48000:d=2", wav)
    item = MediaItem(str(wav), Edit(trim=Trim(start=0.5), speed=Speed(rate=0.5), audio=Audio(volume_db=-6, fade_out=0.3),
                                    export=Export(audio_format=fmt)).validated())
    item.info = probe(ff_tools.ffprobe, wav)
    spec = spec_from_item(item)
    assert spec.kind == AUDIO_JOB and spec.output.suffix == "." + fmt
    out = run_render(spec, ff_tools)
    dur = float(probe_streams(out)["audio"]["duration"])
    assert dur == pytest.approx(3.0, abs=0.08), "1.5 s left after the trim, at half speed"
    assert onset_seconds(audio_samples(out)) == pytest.approx(1.0, abs=0.06)


def test_extract_audio_from_video_and_mute(ff_tools, tmp_path):
    src = make_index_video(tmp_path / "v.mp4", frames=25, audio="tone")
    item = MediaItem(str(src), Edit().with_section("enhance", upscale=False))
    out = run_render(spec_from_item(item, audio_only=True), ff_tools)
    assert "video" not in probe_streams(out) and "audio" in probe_streams(out)
    item.edit = item.edit.with_section("audio", mute=True)
    muted = run_render(spec_from_item(item), ff_tools)
    assert "audio" not in probe_streams(muted)


# --- video export formats -------------------------------------------------------------------------------------
def video_spec(src, out, **sections):
    sections.setdefault("enhance", Enhance(upscale=False))
    return RenderSpec(src, out, Edit(**sections).validated())


def test_webm_and_gif_exports(ff_tools, tmp_path):
    src = make_index_video(tmp_path / "v.mp4", rate="25", frames=25, audio="tone")
    enc = __import__("framewise.tools.encoders", fromlist=["x"]).compiled_encoders(ff_tools.ffmpeg)
    if "libvpx-vp9" in enc:
        out = run_render(video_spec(src, tmp_path / "o.webm", export=Export(format="webm", video_codec="libvpx-vp9")),
                         ff_tools)
        st = probe_streams(out)
        assert int(st["video"]["nb_read_frames"]) == 25 and "audio" in st
    gif = run_render(video_spec(src, tmp_path / "o.gif", export=Export(format="gif", fps=10)), ff_tools)
    info = probe(ff_tools.ffprobe, gif)
    assert info.kind == GIF and info.frames == 10 and "audio" not in probe_streams(gif)


def test_export_frame_rate_change(ff_tools, tmp_path):
    src = make_index_video(tmp_path / "v.mp4", rate="25", frames=50)
    out = run_render(video_spec(src, tmp_path / "o.mp4", export=Export(fps=10)), ff_tools)
    st = probe_streams(out)
    assert int(st["video"]["nb_read_frames"]) == 20 and float(st["video"]["duration"]) == pytest.approx(2.0, abs=0.01)


@pytest.mark.parametrize("mode", ["bitrate", "size"])
def test_bitrate_and_target_size(ff_tools, tmp_path, mode):
    src = tmp_path / "noise.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=4", "-c:v", "libx264", "-crf", "10", src)
    export = Export(rate_control=mode, video_bitrate=400, target_mb=0.25)
    out = run_render(video_spec(src, tmp_path / "o.mp4", export=export), ff_tools)
    size_kb = out.stat().st_size * 8 / 1000 / 4
    target = 400 if mode == "bitrate" else 0.25 * 8 * 1024 * 1024 / 1000 / 4
    assert size_kb == pytest.approx(target, rel=0.35), "approximate by design, but in the right range"
    with pytest.raises(ToolError, match="too small"):  # 0.1 MB for 16 s (4 s at quarter speed) ≈ 50 kb/s
        run_render(video_spec(src, tmp_path / "x.mp4", speed=Speed(rate=0.25),
                              export=Export(rate_control="size", target_mb=0.1)), ff_tools)


def test_target_size_maths():
    from framewise.render.ffmpeg_ops import target_kbps
    kbps = target_kbps(Export(target_mb=20, audio_bitrate=128), 60, True)
    assert kbps == int(20 * 8 * 1024 * 1024 / 1000 * 0.98 / 60 - 128)
    assert target_kbps(Export(target_mb=20, audio_bitrate=128), 60, False) == kbps + 128


# --- reverse / stabilize ------------------------------------------------------------------------------------------
def test_reverse_video_and_audio(ff_tools, tmp_path):
    src = make_index_video(tmp_path / "v.mp4", rate="25", frames=50, audio="beep")  # tone from 1.0 s
    out = run_render(video_spec(src, tmp_path / "r.mp4", speed=Speed(reverse=True)), ff_tools)
    assert frame_indices(out) == list(range(49, -1, -1))
    samples = audio_samples(out)
    assert onset_seconds(samples) < 0.05, "reversed: the tone now starts at the beginning"
    assert onset_seconds(samples[::-1]) == pytest.approx(1.0, abs=0.05)


def test_stabilization_when_available(ff_tools, tmp_path):
    caps = Capabilities.detect(ff_tools.ffmpeg)
    if not caps.available("stabilize"):
        pytest.skip("this ffmpeg has no vid.stab")
    src = tmp_path / "shaky.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=s=160x120:r=25:d=2", "-vf",
       "crop=128:96:16+12*sin(n/2):12+10*cos(n/3)", "-c:v", "libx264", src)
    s = video_spec(src, tmp_path / "s.mp4", enhance=Enhance(upscale=False, stabilize=True), trim=Trim(start=0.4))
    out = run_render(s, ff_tools)
    assert int(probe_streams(out)["video"]["nb_read_frames"]) == 40


def test_unsupported_ai_combinations_are_refused_clearly(toolset, tmp_path):
    src = make_index_video(tmp_path / "v.mp4", frames=20)
    for sec in (dict(speed=Speed(reverse=True)), dict(enhance=Enhance(upscale=True, preset="anime", stabilize=True))):
        edit = Edit(enhance=Enhance(upscale=True, preset="anime")).validated()
        for k, v in sec.items():
            edit = Edit(**{**{n: getattr(edit, n) for n in ("trim", "transform", "color", "filter", "enhance",
                                                              "speed", "audio", "export")}, k: v}).validated()
        with pytest.raises(ToolError, match="can't be combined with AI upscaling"):
            run_render(RenderSpec(src, tmp_path / "o.mp4", edit), toolset)


@pytest.mark.parametrize("fmt", ["webm", "gif"])
def test_ai_upscale_with_final_encode(toolset, tmp_path, fmt):
    enc = __import__("framewise.tools.encoders", fromlist=["x"]).compiled_encoders(toolset.ffmpeg)
    if fmt == "webm" and "libvpx-vp9" not in enc:
        pytest.skip("no VP9 encoder")
    src = make_index_video(tmp_path / "v.mp4", rate="25", frames=20, audio="tone")
    edit = Edit(enhance=Enhance(upscale=True, preset="anime", target="2x"),
                export=Export(format=fmt, video_codec="libvpx-vp9", fps=10 if fmt == "gif" else 0)).validated()
    out = run_render(RenderSpec(src, tmp_path / f"o.{fmt}", edit, segment_frames=8), toolset)
    st = probe_streams(out)
    assert (int(st["video"]["width"]), int(st["video"]["height"])) == (64, 32)
    assert int(st["video"]["nb_read_frames"]) == (8 if fmt == "gif" else 20)
    assert ("audio" in st) == (fmt == "webm")


# --- preview engine ------------------------------------------------------------------------------------------------
def test_preview_matches_export_and_is_cached(ff_tools, tmp_path):
    src = make_image(tmp_path / "p.png", color="0x606060")
    edit = Edit().with_section("color", temperature=60, contrast=30).with_section("enhance", upscale=False)
    info = probe(ff_tools.ffprobe, src)
    req = PreviewRequest(Source(src), info, edit, max_w=640, max_h=480)
    res = render_preview(req, ff_tools, tmp_path / "cache")
    exported = run_render(image_spec(src, tmp_path / "e.png", edit), ff_tools)
    assert rgb_frames(res.image)[2][0] == rgb_frames(exported)[2][0], "preview pixels == export pixels"
    mtime = res.image.stat().st_mtime_ns
    again = render_preview(req, ff_tools, tmp_path / "cache")
    assert again.image == res.image and again.image.stat().st_mtime_ns >= mtime
    other = render_preview(PreviewRequest(Source(src), info, edit.with_section("color", contrast=0)), ff_tools,
                           tmp_path / "cache")
    assert other.image != res.image, "a different edit must never reuse a cached frame"


def test_preview_scales_down_but_keeps_aspect_and_reports_omissions(ff_tools, tmp_path):
    src = make_index_video(tmp_path / "v.mp4", rate="25", frames=25)
    info = probe(ff_tools.ffprobe, src)
    edit = Edit(enhance=Enhance(upscale=False, stabilize=True), speed=Speed(rate=2),
                transform=Transform(rotate=90)).validated()
    res = render_preview(PreviewRequest(Source(src), info, edit, time=0.5, max_w=8, max_h=8), ff_tools, tmp_path / "c")
    w, h, _ = rgb_frames(res.image)
    assert (w, h) == (4, 8) and res.frame_size == (16, 32)
    assert set(res.omitted) == {"Stabilization", "Speed"}
    orig = render_preview(PreviewRequest(Source(src), info, edit, time=0.5, edited=False), ff_tools, tmp_path / "c")
    assert rgb_frames(orig.image)[:2] == (32, 16)


def test_preview_without_crop_shows_whole_frame(ff_tools, tmp_path):
    src = make_image(tmp_path / "c.png")
    edit = Edit(transform=Transform(crop=CropRect(0, 0, 32, 24)), enhance=Enhance(upscale=False)).validated()
    info = probe(ff_tools.ffprobe, src)
    full = render_preview(PreviewRequest(Source(src), info, edit, without_crop=True), ff_tools, tmp_path / "c")
    cropped = render_preview(PreviewRequest(Source(src), info, edit), ff_tools, tmp_path / "c")
    assert rgb_frames(full.image)[:2] == (64, 48) and rgb_frames(cropped.image)[:2] == (32, 24)


def test_preview_cancellation(ff_tools, tmp_path):
    from framewise.core.errors import Cancelled
    src = make_index_video(tmp_path / "v.mp4", frames=10)
    info = probe(ff_tools.ffprobe, src)
    Capabilities.detect(ff_tools.ffmpeg)  # cached before ffmpeg is replaced below
    ev = threading.Event()
    ev.set()
    import framewise.core.proc as proc
    real = proc.subprocess.Popen

    class Slow(real):  # make ffmpeg slow enough to be cancelled
        def __init__(self, args, **kw):
            super().__init__([__import__("sys").executable, "-c", "import time; time.sleep(5)"], **kw)
    proc.subprocess.Popen = Slow
    try:
        with pytest.raises(Cancelled):
            render_preview(PreviewRequest(Source(src), info, Edit()), ff_tools, tmp_path / "c", cancel=ev)
    finally:
        proc.subprocess.Popen = real


# --- black bars -------------------------------------------------------------------------------------------------------
def test_black_bar_detection_suggests_exact_crop(ff_tools, tmp_path):
    src = tmp_path / "letterbox.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=s=160x80:r=25:d=2", "-vf", "pad=160:120:0:20:black",
       "-c:v", "libx264", "-crf", "8", src)
    info = probe(ff_tools.ffprobe, src)
    rect = detect_black_bars(ff_tools.ffmpeg, Source(src), info, Transform(), samples=3)
    assert rect is not None and abs(rect.y - 20) <= 2 and abs(rect.h - 80) <= 4 and rect.w >= 156
    still = tmp_path / "letterbox.png"
    ff("-f", "lavfi", "-i", "testsrc2=s=160x80:d=0.04", "-vf", "pad=160:120:0:20:black", "-frames:v", "1",
       "-update", "1", still)
    rect = detect_black_bars(ff_tools.ffmpeg, Source(still), probe(ff_tools.ffprobe, still), Transform())
    assert rect is not None and abs(rect.y - 20) <= 2, "still images are measured too"
    plain = tmp_path / "plain.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=s=160x120:r=25:d=1", "-c:v", "libx264", plain)
    assert detect_black_bars(ff_tools.ffmpeg, Source(plain), probe(ff_tools.ffprobe, plain), Transform(), samples=2) is None


# --- awkward paths for the new job types --------------------------------------------------------------------------------
def test_image_and_audio_jobs_with_awkward_paths(ff_tools, tmp_path):
    folder = tmp_path / "Tanish's photos" / "été 日本 v2.0"
    folder.mkdir(parents=True)
    img = make_image(folder / "pic.final.v3.png")
    out = run_render(image_spec(img, folder / "out dir (1)" / "pic.final.v3_edit.jpg", image_format="jpeg"), ff_tools)
    assert out.exists() and probe(ff_tools.ffprobe, out).kind == IMAGE
    wav = folder / "voice.take.2.wav"
    ff("-f", "lavfi", "-i", "sine=d=1", wav)
    item = MediaItem(str(wav), Edit(audio=Audio(fade_in=0.2)).validated())
    assert run_render(spec_from_item(item), ff_tools).exists()
