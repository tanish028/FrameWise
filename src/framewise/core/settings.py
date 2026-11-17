"""User settings stored as JSON. Unknown keys are ignored and missing keys get defaults."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .logging_setup import get_logger

log = get_logger("settings")


@dataclass
class Settings:
    output_dir: str = ""            # empty = next to the source video
    default_encoder: str = "libx264"
    default_quality: int = 18
    last_open_dir: str = ""
    recent_projects: list[str] = field(default_factory=list)

    def add_recent(self, path: str, limit: int = 8) -> None:
        self.recent_projects = [path] + [p for p in self.recent_projects if p != path]
        del self.recent_projects[limit:]

    @classmethod
    def load(cls, path: Path) -> "Settings":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls()
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Ignoring unreadable settings file %s: %s", path, e)
            return cls()
        known = {f.name for f in fields(cls)}
        s = cls()
        for k, v in data.items() if isinstance(data, dict) else ():
            if k in known and isinstance(v, type(getattr(s, k))):
                setattr(s, k, v)
        return s

    def save(self, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            log.warning("Could not save settings: %s", e)
