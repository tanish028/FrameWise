# FrameWise architecture

One shared creative engine, two workspaces (Image Studio, Video Studio). Everything below `ui/` is plain
Python with no Qt, and is tested on its own.

```
src/framewise/
  app.py                 entry point, logging, Qt application
  core/                  errors, paths, settings, logging, run_cancellable (cancel + stall watchdog)
  media/                 MEDIA ENGINE
    probe.py             ffprobe → MediaInfo with a kind: video | image | audio | gif | sequence
    source.py            Source = file or numbered image sequence; input_args() for every ffmpeg call
    thumbnails.py        cached poster frames
  project/               PROJECT ENGINE + HISTORY
    edit.py              Edit = immutable sections (trim, transform, color, filter, enhance, speed, audio, export)
    model.py             MediaItem (path, edit, info, sequence), Project
    io.py, migrations.py versioned .fwproj files, v1 → v2 migration, atomic saves
    undo.py, document.py undo stack; every edit is one command; widgets react to document events
  render/                EDITING + COLOUR + EXPORT ENGINE
    ops.py               the operations as filter builders (colour, tone curve, looks, sharpen, blur,
                         denoise, deinterlace, stabilize, canvas fit/fill/blur/stretch, GIF, audio levels/fades)
    graph.py             build_plan(): Edit + MediaInfo → RenderPlan (ordered filters split at the
                         AI boundary, frame windows, timing, audio chain, output format)
    ffmpeg_ops.py        ffmpeg command builders: export, 2-pass size, mux, chunks, images, audio
    pipeline.py          run_render(): video (single pass or chunked AI), image, audio jobs
    preview.py           PREVIEW ENGINE: one processed frame per request, cached on disk
    analysis.py          black-bar detection (suggestions only)
    queue.py             TASK MANAGER for renders: background worker, progress, ETA, cancel, retry
    upscaler.py, presets.py, scaling.py   AI ENGINE (Real-ESRGAN ncnn-vulkan)
  tools/                 locate/download ffmpeg + Real-ESRGAN, encoder detection, ffmpeg capability detection
  ui/                    Midnight Studio (PySide6): theme, icons, components, studios, panels
```

## Key decisions

1. **One plan for every output.** Exports, chunked AI renders, image exports and preview frames all take
   their filters from `build_plan`. A filter string is never built anywhere else, so the preview cannot
   disagree with the export.
2. **Images use the video engine.** A still image is a one-frame plan (`still=True`): the same crop,
   colour, looks, sharpen and resize code, with timing, audio and stabilization left out. Only container
   encoding differs (PNG, JPEG, WebP, BMP).
3. **Fixed filter order.** Deinterlace → rotate/flip → crop → denoise → stabilize → **AI upscale** →
   resize → canvas → colour → look → sharpen → blur → vignette → reverse → speed → frame rate → GIF palette.
   Stages before the AI boundary run while frames are extracted; stages after it run while chunks are encoded.
4. **Previews are processed at full resolution, then scaled down for display**, so radius-dependent
   effects (sharpen, blur, vignette) match the export. Anything a still frame cannot show (speed,
   stabilization) is reported, never faked. AI upscaling in the preview is opt-in.
5. **Audio is always filtered in its own ffmpeg process** when it needs filtering (an ffmpeg 8 deadlock),
   padded *before* stretching and cut to the exact video length after it.
6. **Capability detection, not assumptions.** Each feature lists the ffmpeg filters it needs. Missing ones
   are shown in Tools → Download / check tools, and renders fail with a plain explanation.
7. **Non-destructive.** Sources are only ever read. Outputs are written as `.part` files and renamed
   when complete. Work folders sit next to the output and are removed when the job finishes.
8. **History granularity.** Sliders preview live while dragging and create one undo step on release.

## Threads
* UI thread: widgets only.
* `TaskRunner` (thread pool): probing, thumbnails, encoder/capability detection, preview frames. The
  preview uses a "latest request wins" token, so stale frames are dropped.
* `RenderQueue` worker: one render at a time, cancellable, resumable for AI chunks.

## Phase 2 limits by design
* Stabilization and reverse are not combined with AI upscaling (refused with an explanation).
* Reverse holds the clip in memory; very long clips are refused with the maximum length that fits.
* Background removal is not included. It needs a large extra model and runtime, planned for a later phase.
