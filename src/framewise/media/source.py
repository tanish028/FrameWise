"""Where frames come from: a single file, or a numbered image sequence.

Every ffmpeg command takes its input options from `input_args`, so sequences need no special
cases in the render code. Paths are always separate arguments; the only string building is the
image2 pattern, where literal '%' characters in the folder or file name are escaped as '%%'.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path

from .probe import IMAGE_EXTENSIONS

_NUMBERED = re.compile(r"^(?P<prefix>.*?)(?P<digits>\d+)$")
MIN_SEQUENCE = 2


@dataclass(frozen=True)
class SequenceInfo:
    pattern: str        # absolute image2 pattern, e.g. C:/shots/frame_%04d.png (literal % escaped)
    start: int          # first frame number
    count: int          # contiguous frames found
    fps: str = "25/1"   # sequences carry no timing; the user can change this

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SequenceInfo | None":
        try:
            fps = Fraction(str(d.get("fps") or "25/1"))
            if fps <= 0:
                fps = Fraction(25)
            return cls(str(d["pattern"]), int(d["start"]), max(1, int(d["count"])),
                       f"{fps.numerator}/{fps.denominator}")
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return None

    def frame_path(self, n: int) -> Path:
        """Path of frame number n (Python's % formatting matches image2's: '%%' → '%', '%04d' → 0007)."""
        return Path(self.pattern % n)


def _escape(text: str) -> str:
    return text.replace("%", "%%")


def detect_sequence(first: Path, fps: str = "25/1") -> SequenceInfo | None:
    """If `first` is one of several numbered images (shot_0001.png, shot_0002.png, ...), describe the run
    of contiguous frames that starts at it. Returns None for a lone image."""
    first = Path(first)
    if first.suffix.lower() not in IMAGE_EXTENSIONS:
        return None
    m = _NUMBERED.match(first.stem)
    if not m:
        return None
    prefix, digits = m.group("prefix"), m.group("digits")
    width, start = len(digits), int(digits)
    count = 0
    while (first.with_name(f"{prefix}{str(start + count).zfill(width)}{first.suffix}")).is_file():
        count += 1
    if count < MIN_SEQUENCE:
        return None
    pattern = _escape(first.parent.as_posix()) + "/" + _escape(prefix) + f"%0{width}d" + _escape(first.suffix)
    return SequenceInfo(pattern, start, count, fps)


@dataclass(frozen=True)
class Source:
    path: Path
    sequence: SequenceInfo | None = None

    def input_args(self) -> list:
        if self.sequence is None:
            return ["-i", self.path]
        s = self.sequence
        return ["-f", "image2", "-framerate", s.fps, "-start_number", str(s.start), "-i", s.pattern]

    def exists(self) -> bool:
        return Path(self.path).is_file()


def input_args(src: "Path | Source") -> list:
    """Input options for a plain path (Phase 1 behaviour: ['-i', path]) or a Source."""
    return src.input_args() if isinstance(src, Source) else ["-i", src]
