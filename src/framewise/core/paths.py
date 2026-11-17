"""Where FrameWise keeps its data. Set FRAMEWISE_HOME to relocate everything (used by tests)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .. import APP_NAME


def app_data_dir() -> Path:
    override = os.environ.get("FRAMEWISE_HOME")
    if override:
        return Path(override)
    if sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / APP_NAME


def tools_dir() -> Path:
    return app_data_dir() / "tools"


def logs_dir() -> Path:
    return app_data_dir() / "logs"


def cache_dir() -> Path:
    return app_data_dir() / "cache"


def settings_path() -> Path:
    return app_data_dir() / "settings.json"
