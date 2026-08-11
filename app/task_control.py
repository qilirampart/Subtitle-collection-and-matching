from __future__ import annotations

import threading


class TaskControl:
    """Cooperative pause/cancel state shared by a worker and the UI."""

    def __init__(self) -> None:
        self._paused = threading.Event()
        self._cancelled = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def cancel(self) -> None:
        self._cancelled.set()
        self._paused.clear()

    def checkpoint(self) -> bool:
        while self._paused.is_set() and not self._cancelled.is_set():
            self._cancelled.wait(0.15)
        return not self._cancelled.is_set()
