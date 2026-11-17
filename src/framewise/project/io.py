"""Save and load `.fwproj` files (versioned JSON, written atomically)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .. import __version__
from ..core.errors import ProjectError
from .model import Project

FORMAT = "framewise-project"
FORMAT_VERSION = 1
EXTENSION = ".fwproj"


def save_project(project: Project, path: Path) -> None:
    path = Path(path)
    payload = {"format": FORMAT, "version": FORMAT_VERSION, "app_version": __version__,
               **project.to_dict(base_dir=path.parent)}
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise ProjectError(f"Could not save the project: {e}") from e


def load_project(path: Path) -> Project:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ProjectError(f"Project not found: {path}") from e
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProjectError(f"This file is not a readable FrameWise project ({e})") from e
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ProjectError("This file is not a FrameWise project")
    version = data.get("version")
    if not isinstance(version, int) or version > FORMAT_VERSION:
        raise ProjectError("This project was made with a newer FrameWise. Please update the app.")
    try:
        return Project.from_dict(data, base_dir=path.parent)
    except (KeyError, TypeError, ValueError) as e:
        raise ProjectError(f"The project file is damaged ({e})") from e
