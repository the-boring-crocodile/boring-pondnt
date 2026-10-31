"""boring-pondnt-d365's engine: one long-lived PowerShell with d365fo.tools, and jobs.

`Session` keeps one Windows PowerShell 5.1 running with `Import-Module
d365fo.tools` and `Enable-D365Exception` done once (18 s on the dev box). A
call sends a script (base64, one line) and reads one `@@RESULT <json>` line
back; anything else the cmdlets print is collected as noise. Calls run one at
a time; a call that does not answer in time restarts the session.

`Jobs` runs long operations (build, sync, best practice) in their own
PowerShell process with output to a log file. Its own small code, not the
core's jobs. Both use service-kit's independent process containment.
"""

from __future__ import annotations

import base64
from collections import deque
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from boring_service_kit.outcomes import Failed, Refused
from boring_service_kit.process import Tree

from . import JOB_RETENTION_SECONDS
from .job_lifecycle import ProcessJobs

LOAD_SECONDS = 120
CALL_SECONDS_DEFAULT = 60
NOISE_LINES = 20
NOISE_LINE_CHARS = 4096
SESSION_LINE_BYTES = 1024 * 1024
SESSION_QUEUE_LINES = 16
HOST_OUTPUT_CHARS = 4 * 1024 * 1024
TAIL_LINES_DEFAULT, TAIL_LINES_MAX = 40, 400
WAIT_SECONDS_MAX = 60
NAME = re.compile(r"[A-Za-z0-9_.\-]{1,128}")

LOOP = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
try {
    Import-Module d365fo.tools -ErrorAction Stop
    Enable-D365Exception
    $ready = @{ ok = $true; version = (Get-Module d365fo.tools).Version.ToString() }
} catch {
    $ready = @{ ok = $false; error = $_.Exception.Message }
}
[Console]::Out.WriteLine('@@READY ' + (ConvertTo-Json -InputObject $ready -Compress))
[Console]::Out.Flush()
while ($null -ne ($line = [Console]::In.ReadLine())) {
    try {
        $script = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($line))
        $value = & ([scriptblock]::Create($script))
        $json = ConvertTo-Json -InputObject @{ ok = $true; value = $value } -Compress -Depth 6
    } catch {
        $json = ConvertTo-Json -InputObject @{ ok = $false; error = $_.Exception.Message } -Compress
    }
    [Console]::Out.WriteLine('@@RESULT ' + $json)
    [Console]::Out.Flush()
}
"""


def ps_quote(value: str) -> str:
    """A PowerShell single-quoted string."""
    return "'" + value.replace("'", "''") + "'"


def checked_name(kind: str, value: str) -> str:
    if not NAME.fullmatch(value or "") or not value.strip("."):
        raise Refused("invalid_request", f"{kind} must be a plain name (letters, digits, _ . -)", **{kind: value})
    return value


def powershell_argv(executable: str, *, command: str) -> list[str]:
    argv = [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
    encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
    return argv + ["-EncodedCommand", encoded]


class SessionOutputError(Exception):
    """Bounded, non-sensitive reason why session output cannot be trusted."""


class SessionOutput:
    """Finite protocol buffer; overflow fails closed instead of dropping replies."""

    def __init__(self) -> None:
        self.lines: queue.Queue[str | None] = queue.Queue(maxsize=SESSION_QUEUE_LINES)
        self.error: str | None = None

    def read(self, stream: Any) -> None:
        try:
            while True:
                raw = stream.readline(SESSION_LINE_BYTES + 1)
                if len(raw) > SESSION_LINE_BYTES:
                    self.error = "PowerShell output line exceeds the session limit"
                    return
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n") if raw else None
                try:
                    self.lines.put_nowait(line)
                except queue.Full:
                    self.error = "PowerShell output buffer exceeds the session limit"
                    return
                if line is None:
                    return
        except (OSError, ValueError):
            self.error = "PowerShell output could not be read"

    def get(self, timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        while True:
            if self.error:
                raise SessionOutputError(self.error)
            try:
                line = self.lines.get(timeout=min(0.1, max(0, deadline - time.monotonic())))
            except queue.Empty:
                if time.monotonic() >= deadline:
                    raise
                continue
            if self.error:
                raise SessionOutputError(self.error)
            return line


def session_reply(line: str, prefix: str) -> dict[str, Any]:
    try:
        reply = json.loads(line[len(prefix):])
    except (ValueError, RecursionError):
        raise SessionOutputError("PowerShell returned malformed session JSON") from None
    if not isinstance(reply, dict) or type(reply.get("ok")) is not bool:
        raise SessionOutputError("PowerShell returned an invalid session reply")
    return reply


class Session:
    """One PowerShell with d365fo.tools loaded; started in the background."""

    def __init__(self, executable: str, call_seconds: float = CALL_SECONDS_DEFAULT) -> None:
        self.executable = executable
        self.call_seconds = call_seconds
        self.state: dict[str, Any] = {"state": "not started"}
        self._lock = threading.Lock()
        self._lifecycle = threading.RLock()
        self._closed = False
        self._epoch = 0
        self._tree: Tree | None = None
        self._reader: threading.Thread | None = None
        self._boot_thread: threading.Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._lines = SessionOutput()
        self._ready = threading.Event()

    # lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Admit one startup; a closed or unconfirmed session cannot restart."""
        with self._lifecycle:
            if self._closed:
                raise Failed("unavailable", "the PowerShell session is closed")
            if self._tree is not None or self.state.get("state") == "loading":
                raise Refused("locked", "the PowerShell session already owns a process")
            self._epoch += 1
            epoch = self._epoch
            self._ready.clear()
            self.state = {"state": "loading", "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            self._boot_thread = threading.Thread(target=self._boot, args=(epoch,),
                                                 name="d365-session", daemon=True)
            self._boot_thread.start()

    def _boot(self, epoch: int) -> None:
        started = time.monotonic()
        try:
            # Construction and publication share admission's lock: close either
            # prevents launch or receives the complete tree, never an orphan.
            with self._lifecycle:
                if self._closed or epoch != self._epoch:
                    return
                self._tree = Tree(powershell_argv(self.executable, command=LOOP),
                                  Path.cwd(), dict(os.environ), subprocess.PIPE,
                                  subprocess.DEVNULL, stdin=subprocess.PIPE)
                self._process = self._tree.popen
                output = self._lines = SessionOutput()
                assert self._process.stdout is not None
                self._reader = threading.Thread(target=output.read, args=(self._process.stdout,),
                                                name="d365-output", daemon=True)
                self._reader.start()
            deadline = time.monotonic() + LOAD_SECONDS
            while time.monotonic() < deadline:
                try:
                    line = output.get(timeout=max(0.1, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if line is None:
                    break
                if line.startswith("@@READY "):
                    ready = session_reply(line, "@@READY ")
                    with self._lifecycle:
                        if self._closed or epoch != self._epoch:
                            return
                        if ready["ok"]:
                            self.state = {"state": "ready", "d365fo_tools": ready.get("version"),
                                          "load_seconds": round(time.monotonic() - started, 1)}
                        else:
                            self._stop_process()
                            self.state = {"state": "no_module", "error": ready.get("error")}
                        self._ready.set()
                    return
            raise SessionOutputError("PowerShell did not report ready")
        except Exception as error:
            with self._lifecycle:
                if self._closed or epoch != self._epoch:
                    return
                message = str(error) if isinstance(error, SessionOutputError) else (
                    f"{type(error).__name__}: PowerShell startup failed")
                try:
                    self._output_failed(SessionOutputError(message))
                except Failed:
                    # Cleanup failure is already recorded, with ownership retained.
                    pass

    def _output_failed(self, error: SessionOutputError) -> None:
        with self._lifecycle:
            if self._closed:
                raise Failed("unavailable", "the PowerShell session is closed")
            self.state = {"state": "failed", "error": str(error)}
            try:
                self._stop_process()
            finally:
                self._ready.set()

    def _next_line(self, timeout: float) -> str | None:
        try:
            return self._lines.get(timeout)
        except SessionOutputError as error:
            self._output_failed(error)
            raise Failed("output_invalid", str(error)) from None

    def _stop_process(self) -> None:
        with self._lifecycle:
            self._epoch += 1
            if self._tree is None:
                return
            try:
                self._tree.close()
                if self._reader is not None:
                    self._reader.join(2)
                    if self._reader.is_alive():
                        raise TimeoutError("session reader did not exit")
                process = self._tree.popen
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()
            except Exception as error:
                self.state = {"state": "failed", "cleanup_confirmed": False,
                              "error": f"{type(error).__name__}: cleanup unconfirmed"}
                self._ready.set()
                raise Failed("cleanup_unconfirmed", "PowerShell tree cleanup was not confirmed") from None
            self._tree = None
            self._process = None

    def close_admission(self) -> None:
        with self._lifecycle:
            self._closed = True

    def close(self) -> None:
        with self._lifecycle:
            self._closed = True  # close admission even if cleanup fails
            try:
                self._stop_process()
                self.state = {"state": "closed", "cleanup_confirmed": True}
            finally:
                self._ready.set()
            boot = self._boot_thread
        if boot is not None and boot is not threading.current_thread():
            boot.join(2)
            if boot.is_alive():
                self.state = {"state": "failed", "cleanup_confirmed": False,
                              "error": "session startup thread did not exit"}
                raise Failed("cleanup_unconfirmed", "PowerShell startup thread did not exit")

    # calls ---------------------------------------------------------------

    def ensure_ready(self, wait: float = 0) -> None:
        if wait:
            self._ready.wait(wait)
        if self._closed:
            raise Failed("unavailable", "the PowerShell session is closed")
        state = self.state.get("state")
        if state == "ready":
            if self._lines.error:
                error = SessionOutputError(self._lines.error)
                self._output_failed(error)
                raise Failed("output_invalid", str(error))
            return
        if state == "no_module":
            raise Refused("not_found", "d365fo.tools is not installed for this user; ask the user to install it "
                          "(Install-Module d365fo.tools) -- this server does not install it", error=self.state.get("error"))
        if state == "loading":
            raise Refused("locked", "d365fo.tools is still loading (about 20 s after start); try again shortly",
                          since=self.state.get("since"))
        raise Failed("unavailable", "the PowerShell session is not running", session=self.state)

    def call(self, script: str, timeout: float | None = None, with_host: bool = False) -> Any:
        """Run `script` in the session; its output (objects) comes back as JSON data.

        with_host: return (value, host_lines) -- what the cmdlets wrote straight to
        the host (d365fo.tools does that for -OutputCommandOnly), not through a stream.

        `timeout` bounds the whole call, waiting for the shared session included
        Not sent within it: Refused `busy`, the command never ran.
        Sent, then no answer within it: Failed `timeout`, its effect unknown.
        """
        timeout = timeout or self.call_seconds
        deadline = time.monotonic() + timeout
        if not self._lock.acquire(timeout=timeout):
            raise Refused("busy", f"the PowerShell session stayed busy for {timeout:g} s; "
                          "the command was not sent")
        try:
            return self._call_locked(script, timeout, deadline, with_host)
        finally:
            self._lock.release()

    def _call_locked(self, script: str, timeout: float, deadline: float, with_host: bool) -> Any:
        """The call, holding the session lock."""
        self.ensure_ready()
        process = self._process
        if process is None or process.poll() is not None:
            self._stop_process()
            self.start()
            raise Failed("unavailable", "the PowerShell session had ended; it is restarting")
        try:
            with self._lifecycle:
                if self._closed:
                    raise Failed("unavailable", "the PowerShell session is closed")
                if time.monotonic() >= deadline:  # last point at which nothing has run
                    raise Refused("busy", f"the PowerShell session stayed busy for {timeout:g} s; "
                                  "the command was not sent")
                assert process.stdin is not None
                process.stdin.write(base64.b64encode(script.encode("utf-8")) + b"\n")
                process.stdin.flush()
        except (OSError, ValueError):
            self._output_failed(SessionOutputError("PowerShell command pipe failed"))
            raise Failed("unavailable", "PowerShell command pipe failed") from None
        noise: deque[str] = deque(maxlen=NOISE_LINES)
        host: list[str] = []
        host_chars = 0
        while True:
            remaining = deadline - time.monotonic()
            try:
                line = self._next_line(timeout=max(0.05, remaining)) if remaining > 0 else None
            except queue.Empty:
                line = None
            if line is None:
                self._stop_process()
                self.start()
                raise Failed("timeout", f"no answer from d365fo.tools within {timeout:g} s; the command was "
                             "sent and its effect is unknown; the session restarts",
                             dispatched=True, noise=list(noise))
            if line.startswith("@@RESULT "):
                try:
                    result = session_reply(line, "@@RESULT ")
                except SessionOutputError as error:
                    self._output_failed(error)
                    raise Failed("output_invalid", str(error)) from None
                if not result.get("ok"):
                    raise Failed("command_failed", result.get("error") or "the command failed",
                                 noise=list(noise))
                return (result.get("value"), host) if with_host else result.get("value")
            if line.strip():
                noise.append(line[:NOISE_LINE_CHARS])
                if with_host:
                    host_chars += len(line) + 1
                    if host_chars > HOST_OUTPUT_CHARS:
                        error = SessionOutputError("PowerShell host output exceeds the session limit")
                        self._output_failed(error)
                        raise Failed("output_invalid", str(error))
                    host.append(line)


def check_tail_lines(lines: int) -> None:
    if not 1 <= lines <= TAIL_LINES_MAX:
        raise Refused("invalid_request", f"tail_lines must be 1-{TAIL_LINES_MAX}")


@dataclass(eq=False)
class Job:
    """One PowerShell job: its record, settled by job_lifecycle.ProcessJobs."""
    job_id: str
    kind: str
    log: str
    started: float
    tree: Tree | None = None
    timeout_seconds: float | None = None  # D365 jobs run until they end or are stopped
    state: str = "running"
    exit_code: int | None = None
    ended: float | None = None
    error: str | None = None
    cleanup_error: str | None = None
    done: threading.Event = field(default_factory=threading.Event)
    stop_requested: threading.Event = field(default_factory=threading.Event)


class Jobs(ProcessJobs[Job]):
    """Long operations, each in its own PowerShell with d365fo.tools, output to a log file."""

    missing = "no such job in this session"

    def __init__(self, executable: str, state_dir: Path) -> None:
        super().__init__()
        self.executable = executable
        self.folder = state_dir / "d365-jobs"
        self._sweep()

    def _sweep(self) -> None:
        # The core job owner's seven-day policy, only for managed records
        # with confirmed terminal settlement. Nothing else in the folder is touched.
        managed = self.folder / "managed"
        if not managed.exists():
            return
        if managed.is_symlink() or (managed.lstat().st_file_attributes & 1024 if os.name == "nt" else False):
            raise Failed("unavailable", "D365 managed log directory must be ordinary")
        cutoff = time.time() - JOB_RETENTION_SECONDS
        for directory in managed.iterdir():
            if (not re.fullmatch(r"[0-9a-f]{12}", directory.name) or directory.is_symlink()
                    or (directory.lstat().st_file_attributes & 1024 if os.name == "nt" else False)):
                continue
            marker = directory / "ended"
            if marker.is_file() and not marker.is_symlink() and marker.stat().st_mtime < cutoff:
                shutil.rmtree(directory)

    def start(self, kind: str, script: str, prepare: Callable[[], None] | None = None) -> dict[str, Any]:
        # Shutdown and spawn/publication use the same admission lock.
        with self._lock:
            if self._closed:
                raise Failed("unavailable", "D365 jobs are closed")
            if kind == "sync":
                pending = self._pending(kind)
                if pending:
                    raise Refused("locked", "a sync has not settled; inspect d365_job", job_id=pending[0])
            if prepare is not None:
                prepare()
            self._sweep()
            managed = self.folder / "managed"
            if self.folder.is_symlink() or (self.folder.exists() and os.name == "nt" and self.folder.lstat().st_file_attributes & 1024):
                raise Failed("unavailable", "D365 log directory must be ordinary")
            managed.mkdir(parents=True, mode=0o700, exist_ok=True)
            if managed.is_symlink() or (managed.lstat().st_file_attributes & 1024 if os.name == "nt" else False):
                raise Failed("unavailable", "D365 managed log directory must be ordinary")
            job_id = uuid.uuid4().hex[:12]
            directory = managed / job_id
            directory.mkdir(mode=0o700)
            log = directory / f"{kind}.log"
            wrapped = ("$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue'; "
                       "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
                       "& { Import-Module d365fo.tools; Enable-D365Exception; " + script + " } *>&1 | "
                       'ForEach-Object { "$_" }')
            job = Job(job_id, kind, str(log), time.time())
            try:
                with os.fdopen(os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
                    job.tree = Tree(powershell_argv(self.executable, command=wrapped),
                                    Path.cwd(), dict(os.environ), handle, subprocess.STDOUT)
            except OSError as error:
                raise Failed("unavailable", f"{type(error).__name__}: cannot start PowerShell") from None
            self._publish(job)
        return self.status(job_id)

    def _settled(self, job: Job) -> None:
        if job.cleanup_error is None:
            # A retention receipt, never command/output/credential content.
            try:
                Path(job.log).with_name("ended").touch(exist_ok=False)
            except OSError:
                pass  # retain diagnostics when retention publication is uncertain

    def _pending(self, kind: str) -> list[str]:
        return [job.job_id for job in self._jobs.values() if job.kind == kind
                and (not job.done.is_set() or job.cleanup_error is not None)]

    def pending(self, kind: str) -> list[str]:
        with self._lock:
            return self._pending(kind)

    def status(self, job_id: str) -> dict[str, Any]:
        job = self._get(job_id)
        with self._lock:
            result: dict[str, Any] = {"job_id": job.job_id, "kind": job.kind, "state": job.state,
                                      "exit_code": job.exit_code, "log": job.log}
            result["seconds"] = round((job.ended or time.time()) - job.started, 1)
            if job.cleanup_error is not None:
                result.update(error=job.cleanup_error, cleanup_confirmed=False)
            elif job.error is not None:
                result["error"] = job.error
            return result

    def wait(self, job_id: str, seconds: float = 0) -> dict[str, Any]:
        """Observe settlement, including cleanup; never restart a command."""
        if not 0 <= seconds <= WAIT_SECONDS_MAX:
            raise Refused("invalid_request", f"wait_seconds must be 0-{WAIT_SECONDS_MAX}")
        done = self._get(job_id).done
        done.wait(seconds)
        return {**self.status(job_id), "wait_expired": not done.is_set()}

    def tail(self, job_id: str, lines: int = TAIL_LINES_DEFAULT) -> list[str]:
        check_tail_lines(lines)
        path = Path(self._get(job_id).log)
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 256 * 1024))
                text = handle.read(min(size, 256 * 1024)).decode("utf-8", errors="replace")
        except OSError:
            return []
        # PowerShell still serializes a few records as CLIXML when its output is redirected: not for reading
        return [line.rstrip("\r") for line in text.split("\n")
                if line.strip() and not line.startswith(("#< CLIXML", "<Objs "))][-lines:]

    def stop(self, job_id: str) -> dict[str, Any]:
        self._stop(job_id)
        return self.status(job_id)

    def close_admission(self) -> None:
        with self._lock:
            self._closed = True

    def shutdown(self) -> None:
        if self._shutdown(25):
            raise Failed("cleanup_unconfirmed", "D365 job cleanup was not confirmed")

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            ids = list(self._jobs)
        return [self.status(job_id) for job_id in reversed(ids)]
