# FrameWise: notes for AI-assisted development

- Layering: `core/ media/ tools/ project/ render/` must never import Qt. Only `ui/` uses PySide6.
- All project edits go through `ProjectDocument` as undoable commands; widgets react to document events.
- A clip's edit is `project.edit.Edit`: immutable sections (trim, transform, color, filter, enhance, speed, audio,
  export) that each repair their own values in `validated()`. Defaults must be neutral (no visible change).
- Changing the saved format: bump `FORMAT_VERSION` in `project/io.py` and add a step to `project/migrations.py`,
  with a fixture file in `tests/data/` written by the old version.
- External programs run via `core.proc.run_cancellable` (cancellable, no pipe deadlocks). Never call subprocess directly.
- User-facing failures raise `FrameWiseError` subclasses with plain-language messages.
- Every new feature ships with tests. GPU work is tested with the fake upscaler in `tests/conftest.py`.
- Run tests: `QT_QPA_PLATFORM=offscreen FRAMEWISE_NO_AUDIO=1 pytest -q`.
- Roadmap phases are listed in README.md. Keep the README's "What works today" section accurate.
