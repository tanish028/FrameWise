# FrameWise: notes for AI-assisted development

- Layering: `core/ media/ tools/ project/ render/` must never import Qt. Only `ui/` uses PySide6.
- All project edits go through `ProjectDocument` as undoable commands; widgets react to document events.
- A clip's edit is `project.edit.Edit`: immutable sections (trim, transform, color, filter, enhance, speed, audio,
  export) that each repair their own values in `validated()`. Defaults must be neutral (no visible change).
- Changing the saved format: bump `FORMAT_VERSION` in `project/io.py` and add a step to `project/migrations.py`,
  with a fixture file in `tests/data/` written by the old version.
- All ffmpeg filters come from `render/graph.py:build_plan` (one RenderPlan for single-pass, chunked AI and
  preview). Add a feature as nodes at the right `Stage`; never build filter strings elsewhere. Pre-AI stages
  (< AI_BOUNDARY) run during frame extraction, post-AI stages when chunks are encoded.
- Frame maths uses `round_half_up` (ffmpeg's rounding), never Python's `round`. Speed changes re-time frame
  indices with integer timestamps (`timing_filters`); chunks pass their global frame offset.
- Every filter a plan can emit must belong to a `tools/capabilities.py` Feature (test enforces this).
- Default edits must keep producing the exact Phase 1 ffmpeg commands (tests/test_graph.py parity tests
  against tests/data/phase1/ffmpeg_ops_phase1.py).
- External programs run via `core.proc.run_cancellable` (cancellable, no pipe deadlocks). Never call subprocess directly.
- User-facing failures raise `FrameWiseError` subclasses with plain-language messages.
- Every new feature ships with tests. GPU work is tested with the fake upscaler in `tests/conftest.py`.
- Run tests: `QT_QPA_PLATFORM=offscreen FRAMEWISE_NO_AUDIO=1 pytest -q`.
- Roadmap phases are listed in README.md. Keep the README's "What works today" section accurate.
