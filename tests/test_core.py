import json
import sys
import threading

import pytest

from framewise.core.errors import Cancelled, ToolError
from framewise.core.proc import run_cancellable
from framewise.core.settings import Settings
from framewise.media.probe import format_duration, format_size, parse_probe_json
from framewise.render.presets import PRESETS, get_preset
from framewise.render.scaling import plan_scaling
from framewise.tools.encoders import ENCODERS, encoder_args


# --- scaling / presets -------------------------------------------------------
def test_factor_target_uses_smallest_sufficient_native_scale():
    p = plan_scaling(640, 360, "2x", (2, 3, 4))
    assert (p.engine_scale, p.out_width, p.out_height, p.needs_resize) == (2, 1280, 720, False)


def test_4x_only_model_is_downscaled_for_2x():
    p = plan_scaling(640, 360, "2x", (4,))
    assert p.engine_scale == 4 and p.needs_resize


def test_height_target_keeps_aspect_with_even_dimensions():
    p = plan_scaling(854, 480, "1080p", (4,))
    assert p.out_height == 1080 and p.out_width % 2 == 0


@pytest.mark.parametrize("target", ["1080p", "1x", "banana"])
def test_invalid_targets(target):
    with pytest.raises(ValueError):
        plan_scaling(1920, 1080, target, (4,))


def test_presets_well_formed():
    assert get_preset("anime").native_scales == (2, 3, 4)
    assert all(p.model and p.native_scales for p in PRESETS.values())
    with pytest.raises(ValueError):
        get_preset("nope")


def test_every_encoder_has_args():
    for key in ENCODERS:
        assert encoder_args(key, 20)[:2] == ["-c:v", key]
    with pytest.raises(ValueError):
        encoder_args("nope", 20)


# --- subprocess handling -----------------------------------------------------
def test_nonzero_exit_raises_tool_error_with_output():
    with pytest.raises(ToolError, match="boom"):
        run_cancellable([sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"])


def test_missing_program_raises_tool_error():
    with pytest.raises(ToolError):
        run_cancellable(["definitely-not-a-real-program-xyz"])


def test_stall_watchdog_stops_a_hung_tool():
    import time
    t = time.monotonic()
    with pytest.raises(ToolError, match="stopped making progress"):
        run_cancellable([sys.executable, "-c", "import time; time.sleep(30)"], tick=0.05,
                        progress=lambda: "stuck", stall_timeout=0.3)
    assert time.monotonic() - t < 5


def test_stall_watchdog_leaves_progressing_tools_alone():
    counter = iter(range(10**6))
    out = run_cancellable([sys.executable, "-c", "import time; time.sleep(0.6); print('ok')"], tick=0.05,
                          progress=lambda: next(counter), stall_timeout=0.2)
    assert "ok" in out


def test_cancel_kills_child():
    ev = threading.Event()
    threading.Timer(0.3, ev.set).start()
    with pytest.raises(Cancelled):
        run_cancellable([sys.executable, "-c", "import time; time.sleep(30)"], cancel=ev, tick=0.1)


# --- probe parsing -----------------------------------------------------------
def _probe_json(**video):
    v = {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
         "avg_frame_rate": "30000/1001", "nb_frames": "300", "duration": "10.01"}
    v.update(video)
    return json.dumps({"streams": [v, {"codec_type": "audio", "codec_name": "aac"}],
                       "format": {"duration": "10.01", "size": "1000"}})


def test_parse_probe_basic():
    info = parse_probe_json(_probe_json())
    assert (info.width, info.height, info.frames, info.fps_str, info.has_audio) == (1920, 1080, 300, "30000/1001", True)


def test_parse_probe_rotated_phone_video_swaps_dimensions():
    info = parse_probe_json(_probe_json(side_data_list=[{"rotation": -90}]))
    assert (info.width, info.height) == (1080, 1920)


def test_parse_probe_estimates_frames_when_missing():
    info = parse_probe_json(_probe_json(nb_frames="N/A", duration="2.0", avg_frame_rate="25/1"))
    assert info.frames == 50


@pytest.mark.parametrize("text", ["not json", json.dumps({"streams": [{"codec_type": "audio"}]})])
def test_parse_probe_rejects_bad_input(text):
    with pytest.raises(ToolError):
        parse_probe_json(text)


def test_formatting_helpers():
    assert format_duration(65) == "1:05" and format_duration(3725) == "1:02:05"
    assert format_size(500) == "500 B" and format_size(1536) == "1.5 KB"


# --- settings ----------------------------------------------------------------
def test_settings_roundtrip_and_tolerates_garbage(tmp_path):
    p = tmp_path / "s.json"
    s = Settings(default_quality=22)
    s.add_recent("a.fwproj")
    s.add_recent("b.fwproj")
    s.add_recent("a.fwproj")
    s.save(p)
    loaded = Settings.load(p)
    assert loaded.default_quality == 22 and loaded.recent_projects == ["a.fwproj", "b.fwproj"]
    p.write_text('{"default_quality": "high", "unknown": 1}')
    assert Settings.load(p).default_quality == Settings().default_quality
    p.write_text("{{{")
    assert Settings.load(p) == Settings()
