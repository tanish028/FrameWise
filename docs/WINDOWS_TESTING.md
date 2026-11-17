# Windows manual test checklist (Phase 2)

Everything below is covered by automated tests on Linux, and the non-AI parts also on Windows CI with
ffmpeg 9. These are the things only a real Windows PC with a GPU, a screen and speakers can confirm.
Tick each box; if one fails, *Tools → Open logs folder* and send `framewise.log`.

Use a test folder with awkward names, for example `D:\Test Videos\Tanish's clips\été 日本\`.

## 0. Start and update
- [ ] `git pull`, run `run.bat`. The app opens on the Midnight Studio dark theme without errors.
- [ ] *Tools → Download / check tools*: ffmpeg ✔, Real-ESRGAN ✔. Note which "Video features" show ✖ or ◐
      (especially **Speed**, **Stabilization** and **Sharpening**).
- [ ] Open a **Phase 1 project** (`.fwproj`). It loads, the settings are kept, and it renders as before.

## 1. Image Studio
- [ ] Import a JPEG and a PNG. The app switches to Image Studio, with thumbnail, size and "IMAGE" badge.
- [ ] Preview appears with the correct aspect ratio. Resize the window: it stays fitted.
- [ ] Rotate left/right/180, flip H/V: the preview updates. Undo/redo (Ctrl+Z / Ctrl+Y) steps back exactly.
- [ ] Crop tool: drag a rectangle, resize with handles, move it, ratio 1:1 lock. Export: the output is
      exactly the crop size shown.
- [ ] Every colour slider: drag (live preview), release (one undo step), type a value, reset button,
      *Reset all colour*. Hover shows the tooltip.
- [ ] Each filter look at 100% and 50% strength.
- [ ] Compare: Edited / Original / Split (drag the divider) / Side by side.
- [ ] Sharpen 100 shows no strong halos. Blur, denoise.
- [ ] Export PNG, JPEG (quality 60), WebP, BMP, and a custom size with Fit / Fill / Blur fill. The
      original file is unchanged (check its date modified).
- [ ] AI upscale 2× on an image with "AI in preview" on (the preview takes a few seconds) and export it.

## 2. Video Studio: editing
- [ ] Import MP4s (landscape, a vertical phone video, one with spaces/Unicode/apostrophes in the path).
- [ ] Play: sound plays and the badge says "Original playback". Pause: the edited frame appears at that position.
- [ ] Trim with the timeline handles, with typed times (`0:05.250`), and with *set to playhead*.
      *Play selection* plays only that part.
- [ ] Speed 0.5×, 2×, 4× and custom 1.3×; *Preview 3 s*: motion and audio are in sync and the pitch sounds normal.
- [ ] Reverse on a short clip: picture and sound play backwards.
- [ ] Stabilize a shaky phone clip (if the feature is ✔). The result is smoother, with zoom hiding the borders.
- [ ] Deinterlace an old camcorder/TV clip if you have one.
- [ ] Detect black bars on a letterboxed video: the suggestion is shown and is applied only after *Apply crop*.
- [ ] Audio: volume −6/+6 dB, mute, normalize, fade in/out 2 s; *Export audio only* (MP3 and WAV).

## 3. Video Studio: export
- [ ] MP4 H.264 at constant quality, then with an NVIDIA/AMD/Intel encoder if listed.
- [ ] WebM (VP9) plays in a browser. GIF (12 fps, 480 px) loops.
- [ ] Presets: YouTube Shorts on a landscape clip (blurred background), Discord (file is under 20 MB),
      WhatsApp (about 15 MB; sends as a normal video).
- [ ] Target size 10 MB on a 1-minute clip: the result is close to 10 MB.
- [ ] Frame rate 30 → 60 and 60 → 30: the duration is unchanged.

## 4. AI video (GPU)
- [ ] AI upscale 2× (anime and general presets): output size as shown in the inspector.
- [ ] AI upscale + crop + colour + speed 0.5× + WebM export.
- [ ] Cancel an AI render mid-way, press *Retry / resume*: it resumes from the finished chunks.
- [ ] AI upscale + stabilize: refused with a clear explanation (expected in Phase 2).

## 5. Robustness
- [ ] Import a corrupt or renamed non-video file: a clear message, no crash.
- [ ] Rename or move a source after importing: shown as "File missing", export refused clearly.
- [ ] Save a project, close, reopen: all edits are back. Undo history starts fresh.
- [ ] Quit while a render runs: you are asked first; the next start is clean.
