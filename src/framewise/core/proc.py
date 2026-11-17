"""Run external tools so they can be cancelled and never deadlock on output pipes."""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable, Sequence

from .errors import Cancelled, ToolError
from .logging_setup import get_logger

log = get_logger("proc")


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def run_cancellable(
    cmd: Sequence[str | os.PathLike],
    *,
    cancel: threading.Event | None = None,
    on_tick: Callable[[], None] | None = None,
    cwd: str | os.PathLike | None = None,
    tick: float = 0.5,
    expect_failure: bool = False,
) -> str:
    """Run cmd and return its combined stdout/stderr.

    Raises ToolError on a non-zero exit (logged as an error unless `expect_failure`)
    and Cancelled if `cancel` is set while running;
    the child process is always killed if this function does not return normally.
    """
    args = [str(c) for c in cmd]
    log.debug("run: %s", " ".join(args))
    with tempfile.TemporaryFile() as out:
        try:
            p = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 cwd=cwd, creationflags=_creation_flags())
        except OSError as e:
            raise ToolError(f"Could not start {Path(args[0]).name}: {e}") from e
        try:
            while True:
                try:
                    rc = p.wait(timeout=tick)
                    break
                except subprocess.TimeoutExpired:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    if on_tick is not None:
                        on_tick()
        except BaseException:
            if p.poll() is None:
                p.kill()
                p.wait()
            raise
        out.seek(0)
        text = out.read().decode("utf-8", errors="replace")
    if rc != 0:
        tail = "\n".join(text.strip().splitlines()[-8:])
        (log.debug if expect_failure else log.error)("%s failed (%s): %s", Path(args[0]).name, rc, tail)
        raise ToolError(f"{Path(args[0]).name} exited with code {rc}:\n{tail}")
    return text
