import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("FRAMEWISE_NO_AUDIO", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not on PATH")

# Stand-in for realesrgan-ncnn-vulkan: scales every PNG with ffmpeg so the pipeline runs without a GPU.
FAKE_UPSCALER = """#!{python}
import pathlib, subprocess, sys
a = sys.argv
get = lambda k: a[a.index(k) + 1]
src, dst, s = pathlib.Path(get("-i")), pathlib.Path(get("-o")), int(get("-s"))
dst.mkdir(parents=True, exist_ok=True)
subprocess.run([{ffmpeg!r}, "-v", "error", "-y", "-i", str(src / "%08d.png"),
                "-vf", f"scale=iw*{{s}}:ih*{{s}}", "-start_number", "1", str(dst / "%08d.png")], check=True)
"""


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "fw_home"
    monkeypatch.setenv("FRAMEWISE_HOME", str(home))
    return home


def make_video(path: Path, size="64x48", rate=20, seconds=1, audio=True) -> Path:
    cmd = [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest"]
    cmd += ["-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)
    return path


@pytest.fixture
def sample_video(tmp_path):
    if not FFMPEG:
        pytest.skip("ffmpeg not on PATH")
    return make_video(tmp_path / "clip.mp4")


@pytest.fixture
def ff_tools():
    """ffmpeg/ffprobe without an AI upscaler: works on every OS, including Windows CI."""
    if not (FFMPEG and FFPROBE):
        pytest.skip("ffmpeg not on PATH")
    from framewise.tools.registry import ToolSet
    return ToolSet(Path(FFMPEG), Path(FFPROBE), None)


@pytest.fixture
def get_tools(request):
    """get_tools(ai) → ff_tools, or the fake-upscaler toolset when the AI path is needed (POSIX only)."""
    return lambda ai: request.getfixturevalue("toolset" if ai else "ff_tools")


@pytest.fixture
def toolset(tmp_path):
    if not (FFMPEG and FFPROBE):
        pytest.skip("ffmpeg not on PATH")
    from framewise.tools.registry import ToolSet

    eng = tmp_path / "engine"
    models = eng / "models"
    models.mkdir(parents=True)
    for name in ("realesr-animevideov3-x2", "realesr-animevideov3-x3", "realesr-animevideov3-x4",
                 "realesrgan-x4plus", "realesrnet-x4plus", "realesrgan-x4plus-anime"):
        (models / f"{name}.param").write_text("stub")
    exe = eng / ("realesrgan-ncnn-vulkan.py" if os.name == "nt" else "realesrgan-ncnn-vulkan")
    exe.write_text(FAKE_UPSCALER.format(python=sys.executable, ffmpeg=FFMPEG))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    if os.name == "nt":
        pytest.skip("fake upscaler script is POSIX-only")
    return ToolSet(Path(FFMPEG), Path(FFPROBE), exe)
