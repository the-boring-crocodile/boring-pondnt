"""Background runs whose answer belongs to whoever collects it.

A tool call has a deadline (a Pond call ends after 30 s); a browser step or a WHS scenario can take
minutes. `Runs.start` hands the work to an executor; `Runs.collect` waits up to a budget and either
returns the run's result, raises its error, or raises `StillRunning` and leaves the run open.

The contract, per key:
- While a run is open -- still running, or finished and not yet collected -- `start` on that key is
  refused with the owner's own `busy` error: a result is never lost, replaced unseen or interleaved
  with another step (review finding F3, 2026-10-25).
- A result or an error counts as collected once a collector receives it; only a wait that runs out
  leaves the run open.
- Collectors that waited for the same run at the same time all receive its outcome; closing removes
  only the run they waited for, never a newer one (review finding F2, 2026-10-25).

Users: d365web's WebClient (one key: its browser), d365whsapp's scenario runs (one key per worker)
and its form mode (the WebClient's run, which carries the worker that started it). Process jobs
(`jobs`, `d365.Jobs`) are records read many times, not answers collected once: `job_lifecycle.ProcessJobs`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable
from concurrent.futures import Executor, Future, wait
from dataclasses import dataclass
from typing import Any


class StillRunning(Exception):
    """The budget ran out; the run goes on and stays open for a later collect."""

    def __init__(self, operation: str, waited: float) -> None:
        super().__init__(operation)
        self.operation, self.waited = operation, waited

    def as_dict(self, hint: str) -> dict[str, Any]:
        return {"status": "running", "operation": self.operation, "waited_seconds": round(self.waited, 1),
                "hint": hint}


@dataclass(eq=False)
class Run:
    label: str
    future: Future
    owner: Any = None  # what the caller keeps with the run (a worker name, a transcript)


class Runs:
    def __init__(self, executor: Executor) -> None:
        self.executor = executor
        self.lock = threading.Lock()  # also held by callers that check `peek` with their own state
        self._open: dict[Hashable, Run] = {}

    def peek(self, key: Hashable) -> Run | None:
        """The key's open run. Hold `lock` when the answer must stay true for a following step."""
        return self._open.get(key)

    def get(self, key: Hashable) -> Run | None:
        with self.lock:
            return self._open.get(key)

    def start(self, key: Hashable, label: str, fn: Callable[..., Any], *args: Any,
              refuse: Callable[[Run], Exception], owner: Any = None,
              admit: Callable[[], None] | None = None) -> Run:
        """Submit fn(*args) as the key's run. While the key has an open run, raise refuse(that run).
        `admit` runs under the same lock first (the caller's own reservations); it raises to refuse."""
        with self.lock:
            current = self._open.get(key)
            if current is not None:
                raise refuse(current)
            if admit is not None:
                admit()
            run = Run(label, self.executor.submit(fn, *args), owner)
            self._open[key] = run
        return run

    def collect(self, key: Hashable, run: Run, budget: float | None) -> Any:
        """The run's result, or its error raised here; either closes it. budget None: until it ends.
        Waiting holds no lock. Raises StillRunning when the budget runs out first."""
        started = time.monotonic()
        wait([run.future], timeout=None if budget is None else max(0.1, budget))
        if not run.future.done():
            raise StillRunning(run.label, time.monotonic() - started)
        with self.lock:
            if self._open.get(key) is run:
                del self._open[key]
        return run.future.result()
