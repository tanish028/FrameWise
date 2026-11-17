"""What the installed ffmpeg can actually do, and which FrameWise features that enables.

ffmpeg builds differ (e.g. some lack vid.stab or rubberband), so features are never assumed:
each declares the filters it needs, and availability is read from `ffmpeg -filters`.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

from ..core.errors import FrameWiseError, ToolError
from ..core.logging_setup import get_logger
from ..core.proc import run_cancellable

log = get_logger("capabilities")


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    needs: tuple[tuple[str, ...], ...]  # all groups must be satisfied; any filter within a group will do
    preferred: tuple[str, ...] = ()     # nice-to-have filters; without them the feature works but worse
    hint: str = ""                      # what the user loses when a preferred filter is missing

    def missing(self, available: frozenset[str]) -> list[str]:
        """Filter names (one per unmet group) that this ffmpeg lacks."""
        return [" or ".join(group) for group in self.needs if not any(f in available for f in group)]

    def degraded(self, available: frozenset[str]) -> bool:
        return not self.missing(available) and any(f not in available for f in self.preferred)


@dataclass(frozen=True)
class FeatureStatus:
    feature: Feature
    missing: list[str]
    degraded: bool

    @property
    def available(self) -> bool:
        return not self.missing

    def describe(self) -> str:
        f = self.feature
        if self.missing:
            return f"✖ {f.label}: unavailable, ffmpeg lacks {', '.join(self.missing)}"
        if self.degraded:
            return f"◐ {f.label}: works, but {f.hint[0].lower() + f.hint[1:] if f.hint else 'with reduced quality'}"
        return f"✔ {f.label}"


# Only features that are implemented are listed; later steps add theirs.
FEATURES: dict[str, Feature] = {f.key: f for f in (
    Feature("encode", "Basic video export", (("scale",), ("format",))),
    Feature("trim", "Trim", (("setpts",), ("asetpts",), ("apad",), ("atrim",))),
    Feature("orient", "Rotate and flip", (("transpose",), ("hflip",), ("vflip",))),
    Feature("crop", "Crop", (("crop",),)),
    Feature("speed", "Speed change (audio pitch kept)",
            (("settb",), ("setpts",), ("fps",), ("tpad",), ("rubberband", "atempo"), ("apad",), ("atrim",)),
            preferred=("rubberband",),
            hint="Without ffmpeg's rubberband filter, sounds in slowed-down audio can arrive up to 0.1 s "
                 "before the picture."),
)}

_REINSTALL_HINT = ("On Windows, delete the 'ffmpeg' folder inside FrameWise's tools folder and use "
                   "Tools → Download tools to get a full build. On Linux/macOS, install a full ffmpeg build.")


class MissingFeatureError(FrameWiseError):
    """The render needs ffmpeg filters this ffmpeg build does not include."""


def parse_filters(text: str) -> frozenset[str]:
    """Filter names from `ffmpeg -filters` output.

    Lines look like ` TS. crop              V->V       Crop the input video.`: a flags column,
    the name, then the pad types. The header block ends with a line of dashes or the flag legend.
    """
    names = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3 or "->" not in parts[2]:
            continue
        flags, name = parts[0], parts[1]
        if set(flags) <= set("TSC.|AVN"):  # flag column
            names.add(name)
    return frozenset(names)


class Capabilities:
    """Cached filter list for one ffmpeg executable."""

    _cache: dict[tuple[str, int], frozenset[str]] = {}
    _lock = threading.Lock()

    def __init__(self, filters: frozenset[str]) -> None:
        self.filters = filters

    @classmethod
    def detect(cls, ffmpeg: Path) -> "Capabilities":
        try:
            key = (str(Path(ffmpeg).resolve()), int(os.stat(ffmpeg).st_mtime))
        except OSError:
            key = (str(ffmpeg), 0)
        with cls._lock:
            cached = cls._cache.get(key)
        if cached is None:
            try:
                cached = parse_filters(run_cancellable([ffmpeg, "-hide_banner", "-filters"]))
            except ToolError as e:
                raise ToolError(f"Could not ask ffmpeg which filters it supports: {e}") from e
            if not cached:
                raise ToolError("ffmpeg reported no filters; the ffmpeg installation looks broken.")
            log.info("ffmpeg at %s has %d filters", ffmpeg, len(cached))
            with cls._lock:
                cls._cache[key] = cached
        return cls(cached)

    def has(self, *filters: str) -> bool:
        return all(f in self.filters for f in filters)

    def feature_status(self) -> list[FeatureStatus]:
        return [FeatureStatus(f, f.missing(self.filters), f.degraded(self.filters)) for f in FEATURES.values()]

    def problems(self) -> list[FeatureStatus]:
        """Features that are unavailable or degraded."""
        return [s for s in self.feature_status() if s.missing or s.degraded]

    def available(self, feature_key: str) -> bool:
        return not FEATURES[feature_key].missing(self.filters)

    def require(self, filters: set[str] | frozenset[str], what: str = "This edit") -> None:
        """Raise a plain-language MissingFeatureError if any filter is unavailable."""
        missing = sorted(f for f in filters if f not in self.filters)
        if missing:
            affected = [f.label for f in FEATURES.values() if any(m in g for g in f.needs for m in missing)]
            names = ", ".join(affected) if affected else "some edits"
            raise MissingFeatureError(
                f"{what} needs ffmpeg filters that your ffmpeg doesn't include: {', '.join(missing)}.\n"
                f"Affected: {names}.\n{_REINSTALL_HINT}")
