"""The lifecycle of process trees that outlive a call, owned once.

Used by the core's command jobs (`jobs.Jobs`) and the D365 server's PowerShell jobs (`d365.Jobs`):
admission lock, registry, one watcher per job as the sole owner of terminal state and cleanup, stop
and shutdown. Each user keeps what differs: how it spawns, where output goes, its status and output
shapes. A job is a record read many times; an answer that belongs to whoever collects it once is
`collect.Runs` instead. Imports nothing of the core's command modules, so the services may use it
(D42).
"""

from __future__ import annotations

import threading
import time
from typing import Generic, Protocol, TypeVar

from boring_service_kit.outcomes import Failed, Refused
from boring_service_kit.process import Tree, settle


class ProcessRecord(Protocol):
    """What ProcessJobs reads and settles on a job record."""
    job_id: str
    tree: Tree | None
    timeout_seconds: float | None
    state: str
    exit_code: int | None
    ended: float | None
    error: str | None
    cleanup_error: str | None
    done: threading.Event
    stop_requested: threading.Event


J = TypeVar("J", bound=ProcessRecord)


class ProcessJobs(Generic[J]):
    """Process trees that outlive one call. Subclasses own their records' start, status and output."""

    missing = "no such job in this server session"
    stop_wait_seconds = 15.0

    def __init__(self) -> None:
        self._jobs: dict[str, J] = {}
        self._lock = threading.RLock()  # admission: spawn, publication and shutdown
        self._closed = False

    def _get(self, job_id: str) -> J:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise Refused("not_found", self.missing, job_id=job_id)
        return job

    def _publish(self, job: J) -> None:
        """Register a spawned job and start its watcher; call under the admission lock. If no
        watcher can start, its settlement runs here and the start fails with the job kept."""
        self._jobs[job.job_id] = job
        try:
            threading.Thread(target=self._watch, args=(job,), name=f"job-{job.job_id[:8]}", daemon=True).start()
        except Exception as error:
            job.stop_requested.set()
            self._watch(job, failed=f"{type(error).__name__}: watcher startup failed")
            raise Failed("start_failed", "job watcher could not start", job_id=job.job_id) from None

    def _watch(self, job: J, failed: str | None = None) -> None:
        """The sole owner of terminal state and tree cleanup. `failed`: the job
        failed before it was watched (its tree is settled all the same); the
        failure is its terminal state, recorded before waiters are released."""
        assert job.tree is not None
        result = settle(job.tree, job.stop_requested, job.timeout_seconds)
        with self._lock:
            job.state, job.ended = ("failed" if failed else result.state), time.time()
            job.exit_code = result.exit_code
            job.error, job.cleanup_error = (failed or result.error), result.cleanup_error
        self._settled(job)
        job.done.set()

    def _settled(self, job: J) -> None:
        """Hook after settlement, before waiters are released."""

    def _request_stop(self, job: J) -> None:
        with self._lock:
            if job.state == "running":
                job.state = "stopping"
                job.stop_requested.set()

    def _stop(self, job_id: str) -> J:
        job = self._get(job_id)
        self._request_stop(job)
        job.done.wait(self.stop_wait_seconds)
        return job

    def _shutdown(self, grace: float) -> list[J]:
        """Close admission, stop every job, wait up to `grace`; the jobs not confirmed settled.
        Their recorded state is kept; nothing is manufactured as stopped."""
        with self._lock:
            self._closed = True
            pending = list(self._jobs.values())
        for job in pending:
            self._request_stop(job)
        deadline = time.monotonic() + grace
        for job in pending:
            job.done.wait(max(0, deadline - time.monotonic()))
        return [job for job in pending if not job.done.is_set() or job.cleanup_error is not None]
