"""A background render queue: one worker thread runs jobs in order. Qt-free; UIs subscribe to events."""
from __future__ import annotations

import enum
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..core.errors import Cancelled, FrameWiseError
from ..core.logging_setup import get_logger
from ..tools.registry import ToolSet
from .pipeline import Progress, run_render
from .spec import RenderSpec

log = get_logger("queue")


class JobStatus(enum.Enum):
    QUEUED = "Queued"
    RUNNING = "Running"
    DONE = "Done"
    FAILED = "Failed"
    CANCELLED = "Cancelled"

    @property
    def finished(self) -> bool:
        return self in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED)


@dataclass
class Job:
    spec: RenderSpec
    name: str
    preview: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: JobStatus = JobStatus.QUEUED
    progress: Progress | None = None
    error: str = ""
    started_at: float | None = None
    finished_at: float | None = None

    def eta_seconds(self) -> float | None:
        p = self.progress
        if self.status != JobStatus.RUNNING or not p or not self.started_at or p.fraction < 0.03:
            return None
        elapsed = time.monotonic() - self.started_at
        return elapsed / p.fraction - elapsed


Runner = Callable[[RenderSpec, ToolSet, threading.Event, Callable[[Progress], None]], Path]
Listener = Callable[[str, Job], None]  # events: "added", "updated", "removed"


class RenderQueue:
    def __init__(self, tools: Callable[[], ToolSet | None], runner: Runner = run_render) -> None:
        self._tools = tools
        self._runner = runner
        self._jobs: list[Job] = []
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._cancel_current = threading.Event()
        self._current: Job | None = None
        self._stop = False
        self._listeners: list[Listener] = []
        self._thread = threading.Thread(target=self._loop, name="render-queue", daemon=True)
        self._thread.start()

    # -- observation ------------------------------------------------------
    def subscribe(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def _emit(self, event: str, job: Job) -> None:
        for fn in list(self._listeners):
            try:
                fn(event, job)
            except Exception:  # a broken listener must not kill the worker
                log.exception("Queue listener failed")

    def jobs(self) -> list[Job]:
        with self._lock:
            return list(self._jobs)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return next((j for j in self._jobs if j.id == job_id), None)

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._current is not None or any(j.status == JobStatus.QUEUED for j in self._jobs)

    # -- control ----------------------------------------------------------
    def add(self, spec: RenderSpec, name: str, preview: bool = False) -> Job:
        job = Job(spec=spec, name=name, preview=preview)
        with self._lock:
            if preview:  # previews jump the queue so feedback is quick
                idx = next((n for n, j in enumerate(self._jobs) if j.status == JobStatus.QUEUED), len(self._jobs))
                self._jobs.insert(idx, job)
            else:
                self._jobs.append(job)
            self._wake.notify()
        self._emit("added", job)
        return job

    def cancel(self, job_id: str) -> None:
        with self._lock:
            job = next((j for j in self._jobs if j.id == job_id), None)
            if job is None:
                return
            if job is self._current:
                self._cancel_current.set()
                return
            if job.status == JobStatus.QUEUED:
                job.status = JobStatus.CANCELLED
        self._emit("updated", job)

    def retry(self, job_id: str) -> None:
        with self._lock:
            job = next((j for j in self._jobs if j.id == job_id), None)
            if job is None or job.status not in (JobStatus.FAILED, JobStatus.CANCELLED):
                return
            job.status, job.error, job.progress = JobStatus.QUEUED, "", None
            job.started_at = job.finished_at = None
            self._wake.notify()
        self._emit("updated", job)

    def remove(self, job_id: str) -> None:
        with self._lock:
            job = next((j for j in self._jobs if j.id == job_id), None)
            if job is None or job is self._current:
                return
            self._jobs.remove(job)
        self._emit("removed", job)

    def clear_finished(self) -> None:
        with self._lock:
            gone = [j for j in self._jobs if j.status.finished]
            self._jobs = [j for j in self._jobs if not j.status.finished]
        for j in gone:
            self._emit("removed", j)

    def shutdown(self, timeout: float = 5.0) -> None:
        with self._lock:
            self._stop = True
            self._cancel_current.set()
            self._wake.notify()
        self._thread.join(timeout)

    # -- worker -----------------------------------------------------------
    def _next_job(self) -> Job | None:
        with self._lock:
            while not self._stop:
                job = next((j for j in self._jobs if j.status == JobStatus.QUEUED), None)
                if job is not None:
                    job.status = JobStatus.RUNNING
                    job.started_at = time.monotonic()
                    self._current = job
                    self._cancel_current.clear()
                    return job
                self._wake.wait()
            return None

    def _loop(self) -> None:
        while (job := self._next_job()) is not None:
            self._emit("updated", job)
            try:
                tools = self._tools()
                if tools is None:
                    raise FrameWiseError("ffmpeg is not installed. Open Tools → Download tools.")

                def on_progress(p: Progress, j: Job = job) -> None:
                    j.progress = p
                    self._emit("updated", j)

                self._runner(job.spec, tools, self._cancel_current, on_progress)
                job.status = JobStatus.DONE
            except Cancelled:
                job.status = JobStatus.CANCELLED
                job.error = "Cancelled. Retry resumes from the last finished chunk." if job.spec.upscale else ""
            except (FrameWiseError, ValueError) as e:
                job.status, job.error = JobStatus.FAILED, str(e)
                log.error("Job %s failed: %s", job.name, e)
            except Exception as e:  # unexpected bug: record it, keep the queue alive
                job.status, job.error = JobStatus.FAILED, f"Unexpected error: {e}"
                log.exception("Job %s crashed", job.name)
            finally:
                job.finished_at = time.monotonic()
                with self._lock:
                    self._current = None
                self._emit("updated", job)
