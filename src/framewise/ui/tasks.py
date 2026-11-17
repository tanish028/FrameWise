"""Run blocking work off the UI thread and get the result back on it."""
from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from ..core.logging_setup import get_logger

log = get_logger("ui.tasks")


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(object)


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as e:  # delivered to on_error on the UI thread
            self.signals.failed.emit(e)
        else:
            self.signals.done.emit(result)


class TaskRunner(QObject):
    """Owns a thread pool; keeps tasks alive until their callbacks have run."""

    def __init__(self, parent: QObject | None = None, max_threads: int = 3) -> None:
        super().__init__(parent)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max_threads)
        self._live: set[_Task] = set()

    def run(self, fn: Callable[[], Any], on_done: Callable[[Any], None] | None = None,
            on_error: Callable[[Exception], None] | None = None) -> None:
        task = _Task(fn)
        task.setAutoDelete(False)
        self._live.add(task)

        def finish(cb, value):
            self._live.discard(task)
            if cb is not None:
                cb(value)
            elif isinstance(value, Exception):
                log.warning("Background task failed: %s", value)

        task.signals.done.connect(lambda r: finish(on_done, r))
        task.signals.failed.connect(lambda e: finish(on_error, e) if on_error else finish(None, e))
        self.pool.start(task)

    def wait(self, msecs: int = 5000) -> bool:
        return self.pool.waitForDone(msecs)
