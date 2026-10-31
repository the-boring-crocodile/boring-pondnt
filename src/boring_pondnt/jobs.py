"""Jobs: commands that outlive one call (builds, DB sync, test runs).

Like the Pond's jobs: a server-minted `job_id`, output in
files, status, a wait over several jobs, output by tail or by line range,
stop, list. Jobs live as long as the server: on exit every tree is killed
(on Windows also by the Job Objects' kill-on-close if the server dies hard).
Output files stay in the state directory and are swept after a week.

Its lifecycle (admission, watcher, stop, shutdown) is `job_lifecycle.ProcessJobs`, shared with
the D365 server's PowerShell jobs.
"""

from __future__ import annotations

import atexit
import secrets
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import execs
from . import JOB_RETENTION_SECONDS as RETENTION_SECONDS
from boring_service_kit.outcomes import Failed, Refused
from boring_service_kit.process import Tree

from .job_lifecycle import ProcessJobs

TIMEOUT_MAX = 86_400.0
WAIT_MAX = 60.0
OUTPUT_LINES_DEFAULT = 100
OUTPUT_LINES_MAX = 2000
OUTPUT_MAX_CHARS = 32 * 1024
LIST_MAX = 100


def check_wait(job_ids: list[str], wait_seconds: float) -> None:
    if not job_ids or len(job_ids) > 32:
        raise Refused("invalid_request", "job_ids must list 1-32 jobs")
    if not 0 <= wait_seconds <= WAIT_MAX:
        raise Refused("invalid_request", f"wait_seconds must be 0-{WAIT_MAX:g}")


def check_output(stream: str, tail_lines: int | None, start_line: int | None,
                 line_count: int) -> int:
    if stream not in ("stdout", "stderr"):
        raise Refused("invalid_request", "stream must be stdout or stderr")
    if tail_lines is not None and start_line is not None:
        raise Refused("invalid_request", "give tail_lines or start_line, not both")
    count = tail_lines if tail_lines is not None else line_count
    if not 1 <= count <= OUTPUT_LINES_MAX:
        raise Refused("invalid_request", f"line counts must be 1-{OUTPUT_LINES_MAX}")
    if start_line is not None and start_line < 1:
        raise Refused("invalid_request", "start_line starts at 1")
    return count


def check_list(limit: int) -> None:
    if not 1 <= limit <= LIST_MAX:
        raise Refused("invalid_request", f"limit must be 1-{LIST_MAX}")


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(eq=False)
class Job:
    job_id: str
    command: str
    shell: str
    cwd: str
    timeout_seconds: float
    directory: Path
    started: float
    tree: Tree | None = None
    state: str = "running"  # running, stopping, exited, timed_out, stopped, failed
    exit_code: int | None = None
    ended: float | None = None
    done: threading.Event = field(default_factory=threading.Event)
    stop_requested: threading.Event = field(default_factory=threading.Event)
    cleanup_error: str | None = None
    error: str | None = None

    def status(self) -> dict[str, Any]:
        end = self.ended if self.ended is not None else time.time()
        result: dict[str, Any] = {
            "job_id": self.job_id,
            "state": self.state,
            "exit_code": self.exit_code,
            "command": self.command if len(self.command) <= 200 else self.command[:200] + "...",
            "shell": self.shell,
            "cwd": self.cwd,
            "started": _iso(self.started),
            "duration_seconds": round(end - self.started, 3),
        }
        if self.error is not None:
            result["error"] = self.error
        if self.cleanup_error is not None:
            result["cleanup_confirmed"] = False
            result["error"] = self.cleanup_error
        for stream in ("stdout", "stderr"):
            path = self.directory / stream
            result[f"{stream}_bytes"] = path.stat().st_size if path.exists() else 0
        return result


class Jobs(ProcessJobs[Job]):
    def __init__(self, state_dir: Path) -> None:
        super().__init__()
        self.root = state_dir / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self._sweep()
        atexit.register(self.shutdown)

    # --- lifecycle -------------------------------------------------------

    def start(self, command: str, shell: str | None = None, cwd: str | None = None,
              timeout_seconds: float = 3600.0) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise Failed("unavailable", "the job manager is closed")
            return self._start(command, shell, cwd, timeout_seconds)

    def _start(self, command: str, shell: str | None, cwd: str | None,
               timeout_seconds: float) -> dict[str, Any]:
        """Admission lock covers construction, publication and watcher startup."""
        chosen = execs.check_command(command, shell, timeout_seconds, TIMEOUT_MAX)
        workdir = execs.working_directory(cwd)
        argv = execs.build_argv(chosen, command)
        job_id = secrets.token_hex(16)
        directory = self.root / job_id
        directory.mkdir()
        (directory / "command.txt").write_text(command, encoding="utf-8")
        job = Job(job_id, command, chosen, str(workdir), timeout_seconds, directory, time.time())
        try:
            with (directory / "stdout").open("wb") as out, (directory / "stderr").open("wb") as err:
                job.tree = Tree(argv, workdir, execs.environment(), out, err)
        except FileNotFoundError:
            shutil.rmtree(directory, ignore_errors=True)
            raise Refused("not_found", f"shell {chosen!r} is not installed") from None
        except OSError as exc:
            shutil.rmtree(directory, ignore_errors=True)
            raise Failed("start_failed", str(exc)) from None
        self._publish(job)
        return job.status()

    def stop(self, job_id: str) -> dict[str, Any]:
        return self._stop(job_id).status()

    def shutdown(self) -> None:
        self._shutdown(15)

    # --- reading -----------------------------------------------------------

    def status(self, job_id: str) -> dict[str, Any]:
        return self._get(job_id).status()

    def wait(self, job_ids: list[str], wait_seconds: float = 30.0) -> dict[str, Any]:
        """Wait until every listed job has ended, or `wait_seconds` pass."""
        check_wait(job_ids, wait_seconds)
        jobs = [self._get(job_id) for job_id in job_ids]
        deadline = time.monotonic() + wait_seconds
        for job in jobs:
            job.done.wait(max(0.0, deadline - time.monotonic()))
        return {"jobs": [job.status() for job in jobs],
                "all_ended": all(job.done.is_set() for job in jobs)}

    def output(self, job_id: str, stream: str = "stdout", tail_lines: int | None = None,
               start_line: int | None = None, line_count: int = OUTPUT_LINES_DEFAULT) -> dict[str, Any]:
        """The last `tail_lines` lines (default), or `line_count` lines from `start_line`."""
        count = check_output(stream, tail_lines, start_line, line_count)
        job = self._get(job_id)
        path = job.directory / stream
        try:
            window = execs.line_window(path, start_line, count, OUTPUT_MAX_CHARS,
                                       execs.oem_code_page())
        except FileNotFoundError:
            window = {"content": "", "start_line": 1, "returned_lines": 0,
                      "total_lines": 0, "content_cut": False}
        return {"job_id": job_id, "stream": stream, "state": job.state, **window}

    def list(self, limit: int = 20) -> dict[str, Any]:
        check_list(limit)
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda job: job.started, reverse=True)
        return {"jobs": [job.status() for job in jobs[:limit]], "total": len(jobs)}

    # --- housekeeping ------------------------------------------------------

    def _sweep(self) -> None:
        cutoff = time.time() - RETENTION_SECONDS
        for directory in self.root.iterdir():
            try:
                if directory.is_dir() and directory.stat().st_mtime < cutoff:
                    shutil.rmtree(directory, ignore_errors=True)
            except OSError:
                pass
