# FrameWise

**Fix and polish photos and videos on your own PC: AI upscaling, colour, crop, trim, speed and export
presets, with nothing uploaded anywhere.**

Free editors make you choose between quick-and-simple tools and AI enhancement. AI enhancement usually
means a cloud service with upload limits and subscriptions, or command-line tools that need CUDA, PyTorch
and patience. FrameWise puts both in one desktop app that runs locally on any NVIDIA, AMD or Intel GPU,
and every edit is non-destructive: your original files are never changed.

![FrameWise Video Studio](docs/screenshot.png)

## What works today (v0.2, Phase 2)

**Two studios, one engine.** *Image Studio* and *Video Studio* share the same editing engine, so a
crop, a colour grade or a look behaves identically on a photo and on a clip. Switch studios in the top
bar; importing a file opens the right one.

| | Image Studio | Video Studio |
|---|---|---|
| Media | PNG, JPEG, WebP, BMP, TIFF | MP4, MOV, MKV, WebM, AVI…, GIF, numbered image sequences, audio files |
| Transform | rotate 90°/180°, flip, crop (freeform or 16:9, 9:16, 1:1, 4:3, 4:5, 21:9), resize with fit / fill / blur fill / stretch | same, plus black-bar detection (suggested, never applied without your OK) |
| Colour | brightness, contrast, saturation, hue, gamma, exposure, temperature, tint, highlights, shadows, whites, blacks, vignette, fade | same |
| Filters | Natural, Cinematic, Warm, Cool, Vintage, Black & White, Vibrant, 3 Color Boost, with a strength slider | same |
| Enhance | sharpen, blur, denoise | sharpen, blur, denoise, deinterlace, two-pass stabilization |
| AI | Real-ESRGAN upscaling 2×/3×/4× or to 1080p/1440p/4K | same, chunked and resumable |
| Time | – | frame-exact trim, 0.25×–4× speed with pitch kept, reverse |
| Audio | – | volume, mute, loudness normalization, fade in/out, export audio only (MP3, M4A, WAV, FLAC) |
| Export | PNG, JPEG, WebP, BMP with quality and size | MP4/MOV (H.264/H.265, NVIDIA/AMD/Intel encoders), WebM (VP9/AV1), GIF; resolution, frame rate, constant quality, bitrate or approximate target size; presets for YouTube, Shorts, Reels, WhatsApp and Discord |

**Preview you can trust.** Paused or scrubbed frames are rendered with exactly the same filters as the
export, at full resolution and then scaled for display. Compare *Edited / Original / Split / Side by
side*. Anything a still frame cannot show (speed, stabilization) is labelled instead of faked. Video
playback shows the original, and the badge says so.

**Built to be relied on.** Undo and redo for every edit (dragging a slider is one step), projects that
open in later versions, a background render queue with progress, time left, cancel and resume, a check
of which features your ffmpeg supports, and a stall watchdog that stops ffmpeg if it hangs.

## Roadmap

| Phase | Scope |
|---|---|
| 1 ✅ | Foundation: library, player, projects, undo, render queue, AI upscaling |
| 2 ✅ | Image Studio + Video Studio: shared editing engine, colour, looks, transform, trim, speed, audio, export studio, preview engine, Midnight Studio UI |
| 3 | Multi-track timeline: arrange and split clips, transitions, music track |
| 4 | Text and subtitles: titles, watermark, SRT burn-in, auto-captions (Whisper) |
| 5 | More AI: smart reframe, frame interpolation (RIFE), scene detection, background removal |
| 6 | Release: Windows installer, GPU auto-configuration |

## Install and run

**Windows:** install [Python 3.10+](https://www.python.org/downloads/) (tick *Add python.exe to PATH*),
clone or download this repository, and double-click `run.bat`. The first launch creates a private Python
environment (about 1–2 minutes). FrameWise then offers to download its tools (about 100 MB):

* **ffmpeg** (BtbN GPL build): decoding, filters and encoding
* **Real-ESRGAN ncnn-vulkan** (with its models): AI upscaling. It needs a GPU with Vulkan support; most
  NVIDIA, AMD and Intel GPUs from the last decade qualify.

Both are stored in `%LOCALAPPDATA%\FrameWise\tools`. *Tools → Download / check tools* shows what is
installed and which editing features your ffmpeg supports.

**Linux / macOS:**
```bash
python -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -m framewise
```
Install ffmpeg with your package manager; FrameWise downloads Real-ESRGAN itself.

**Updating:** `git pull`, then start `run.bat` again. Projects from older versions are upgraded
automatically when opened; the file only changes when you save.

Logs: *Tools → Open logs folder* (`%LOCALAPPDATA%\FrameWise\logs`).

## Known limitations
* Video playback shows the original; edits appear on paused or scrubbed frames and in rendered
  previews (*Preview 3 s*).
* Stabilization and reverse cannot be combined with AI upscaling yet. FrameWise explains this and
  suggests exporting in two steps.
* Reverse holds the clip in memory. Long or very large clips are refused, with the maximum length shown.
* Target-size exports are approximate (usually within a few percent with 2-pass x264/VP9, less exact
  with GPU encoders).
* Without ffmpeg's `rubberband` filter, slowed-down audio can run up to 0.1 s ahead of the picture
  (shown as "limited" in the feature check).
* Colour edits on images with transparency do not keep the alpha channel.
* Background removal is not included yet (planned for Phase 5).
* Image-sequence projects store the sequence's absolute folder path.

## Development
```bash
pip install -e ".[dev]"
QT_QPA_PLATFORM=offscreen FRAMEWISE_NO_AUDIO=1 pytest
```
Around 390 tests run without a GPU (a stand-in upscaler resizes frames with ffmpeg). Most check real
ffmpeg output pixel by pixel: marker blocks for geometry, frames that encode their own index for trim
and speed, timed tones for audio sync and pitch. CI runs on Ubuntu (ffmpeg 8) and Windows (ffmpeg 9)
with Python 3.10 and 3.12.

Architecture: see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Windows test checklist:
[docs/WINDOWS_TESTING.md](docs/WINDOWS_TESTING.md).

## Credits
[ffmpeg](https://ffmpeg.org) · [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) (ncnn-vulkan build) ·
[Qt for Python](https://doc.qt.io/qtforpython/) · [Lucide](https://lucide.dev) icons (ISC licence, see
`src/framewise/ui/icons/LICENSE-lucide.txt`)
