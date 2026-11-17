"""A small, Qt-free undo/redo stack built on the command pattern."""
from __future__ import annotations

from typing import Callable, Protocol


class Command(Protocol):
    text: str

    def do(self) -> None: ...
    def undo(self) -> None: ...


class UndoStack:
    def __init__(self, limit: int = 200) -> None:
        self._done: list[Command] = []
        self._undone: list[Command] = []
        self._limit = limit
        self._clean: int | None = 0  # len(_done) at last save; None = unreachable
        self._listeners: list[Callable[[], None]] = []

    def subscribe(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def _changed(self) -> None:
        for fn in list(self._listeners):
            fn()

    def push(self, cmd: Command) -> None:
        """Execute cmd and record it. If do() raises, nothing is recorded."""
        cmd.do()
        if self._clean is not None and self._clean > len(self._done):
            self._clean = None  # the saved state was in the redo branch we are discarding
        self._done.append(cmd)
        self._undone.clear()
        if len(self._done) > self._limit:
            self._done.pop(0)
            if self._clean is not None:
                self._clean -= 1
                if self._clean < 0:
                    self._clean = None
        self._changed()

    def undo(self) -> bool:
        if not self._done:
            return False
        cmd = self._done.pop()
        cmd.undo()
        self._undone.append(cmd)
        self._changed()
        return True

    def redo(self) -> bool:
        if not self._undone:
            return False
        cmd = self._undone.pop()
        cmd.do()
        self._done.append(cmd)
        self._changed()
        return True

    def can_undo(self) -> bool:
        return bool(self._done)

    def can_redo(self) -> bool:
        return bool(self._undone)

    def undo_text(self) -> str:
        return self._done[-1].text if self._done else ""

    def redo_text(self) -> str:
        return self._undone[-1].text if self._undone else ""

    def set_clean(self) -> None:
        self._clean = len(self._done)
        self._changed()

    def is_clean(self) -> bool:
        return self._clean == len(self._done)

    def clear(self) -> None:
        self._done.clear()
        self._undone.clear()
        self._clean = 0
        self._changed()
