"""A small job queue so batches run a few at a time, with progress and cancel per job."""

from __future__ import annotations

import enum
import itertools
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .convert import convert_file
from .runner import Cancelled, ConversionError
from .tools import run_tool, tool

# Two at once keeps the desktop responsive; ffmpeg and vips already use many threads each.
DEFAULT_WORKERS = 2

log = logging.getLogger("hala")


class Status(enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self not in (Status.QUEUED, Status.RUNNING)


@dataclass
class Job:
    id: int
    src: Path
    target: str
    outdir: Path | None = None
    status: Status = Status.QUEUED
    progress: float = 0.0
    output: Path | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)  # see runner.Context.notes
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    # Tool jobs: target is the tool's name; a combining tool (merge, join) gets every input
    # in one job, with src being the first.
    tool: bool = False
    inputs: list[Path] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)

    def cancel(self) -> None:
        self.cancel_event.set()


Listener = Callable[[Job], None]


class JobQueue:
    def __init__(self, workers: int = DEFAULT_WORKERS, listener: Listener | None = None):
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hala")
        self._ids = itertools.count(1)
        self._listener = listener or (lambda _job: None)

    def submit(self, src: Path, target: str, outdir: Path | None = None) -> tuple[Job, Future[Job]]:
        job = Job(next(self._ids), src, target, outdir)
        return job, self._pool.submit(self._run, job)

    def submit_tool(self, name: str, inputs: list[Path], params: dict[str, Any] | None = None,
                    outdir: Path | None = None) -> tuple[Job, Future[Job]]:
        job = Job(next(self._ids), inputs[0], name, outdir, tool=True, inputs=list(inputs),
                  params=dict(params or {}))
        return job, self._pool.submit(self._run, job)

    def _run(self, job: Job) -> Job:
        if job.cancel_event.is_set():
            job.status = Status.CANCELLED
            self._listener(job)
            return job
        job.status = Status.RUNNING
        self._listener(job)

        def on_progress(fraction: float) -> None:
            job.progress = fraction
            self._listener(job)

        try:
            if job.tool:
                output = run_tool(tool(job.target), job.inputs, job.params, outdir=job.outdir,
                                  on_progress=on_progress, cancel_event=job.cancel_event,
                                  notes=job.notes)
            else:
                output = convert_file(job.src, job.target, outdir=job.outdir,
                                      on_progress=on_progress, cancel_event=job.cancel_event,
                                      notes=job.notes)
            job.output, job.status = output, Status.DONE
        except Cancelled:
            job.status = Status.CANCELLED
        except ConversionError as exc:
            log.error("%s -> %s failed:\n%s", job.src, job.target, exc)
            job.error, job.status = str(exc), Status.FAILED
        except Exception as exc:  # an engine bug shouldn't take the queue down
            log.exception("%s -> %s crashed", job.src, job.target)
            job.error, job.status = f"{type(exc).__name__}: {exc}", Status.FAILED
        self._listener(job)
        return job

    def shutdown(self, cancel: bool = False) -> None:
        self._pool.shutdown(wait=True, cancel_futures=cancel)
