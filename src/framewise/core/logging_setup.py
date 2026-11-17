"""Logging: rotating file in the app data folder, plus a hook for uncaught exceptions."""
from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
from pathlib import Path

ROOT = "framewise"
FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"{ROOT}.{name}" if name else ROOT)


def setup_logging(log_dir: Path | None, level: int = logging.INFO, console: bool = False) -> logging.Logger:
    log = logging.getLogger(ROOT)
    log.setLevel(level)
    for h in list(log.handlers):
        log.removeHandler(h)
        h.close()
    fmt = logging.Formatter(FORMAT)
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(
                log_dir / "framewise.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
            fh.setFormatter(fmt)
            log.addHandler(fh)
        except OSError as e:  # never let logging stop the app
            print(f"FrameWise: file logging disabled ({e})", file=sys.stderr)
    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        log.addHandler(sh)
    if not log.handlers:
        log.addHandler(logging.NullHandler())
    return log


def install_excepthook() -> None:
    """Log uncaught exceptions from the main thread and worker threads."""
    log = get_logger("crash")

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    sys.excepthook = hook
    threading.excepthook = lambda args: hook(args.exc_type, args.exc_value, args.exc_traceback)
