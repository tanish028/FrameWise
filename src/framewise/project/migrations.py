"""Upgrade older project files, one format version at a time, before they are parsed.

Each migration takes the raw JSON dict of version N and returns the dict of version N+1.
Migrations never touch the user's file on disk; the upgraded project is written only when
the user saves.
"""
from __future__ import annotations

import copy
from typing import Callable


def _v1_to_v2(data: dict) -> dict:
    """v1 stored a flat `settings` record per item; v2 stores a sectioned `edit`."""
    out = copy.deepcopy(data)
    for item in out.get("items") or []:
        if not isinstance(item, dict):
            continue
        s = item.pop("settings", None)
        if not isinstance(s, dict):
            s = {}
        enhance = {k: s[k] for k in ("upscale", "preset", "target") if k in s}
        export = {}
        if "encoder" in s:
            export["video_codec"] = s["encoder"]
        for k in ("quality", "output"):
            if k in s:
                export[k] = s[k]
        item["edit"] = {"enhance": enhance, "export": export}
    out["version"] = 2
    return out


MIGRATIONS: dict[int, Callable[[dict], dict]] = {1: _v1_to_v2}


def migrate(data: dict, to_version: int) -> dict:
    """Apply migrations until `data` is at `to_version`. Raises ValueError if a step is missing."""
    version = data.get("version")
    while version < to_version:
        step = MIGRATIONS.get(version)
        if step is None:
            raise ValueError(f"no migration from project version {version}")
        data = step(data)
        version = data["version"]
    return data
