"""`boring-pondnt-core --self-check`: does pondNT work on this machine?

Dev environments differ, so this checks behaviour, live, with
the same code the tools use: each shell's exit code and UTF-8 round trip, a
timeout that returns, a text file edited without changing its encoding or
line endings, a job started and waited for, the state directory, long paths.
Prints one JSON report; exit code 0 when every required check passed.
Nothing outside a temporary folder and the state directory is touched.
"""

from __future__ import annotations

import html
import json
import platform
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from . import __version__, execs, jobs, textfiles
from boring_service_kit.process import IS_WINDOWS

MARKER = "Grüße äöü ✓"
MARKER_CMD = "Grüße äöü"


def _shell_check(shell: str) -> str:
    if shell in ("pwsh", "powershell"):
        command = f"Write-Output '{MARKER}'; cmd /c exit 3"
    elif shell == "cmd":
        # cmd's echo writes the OEM code page, which has no checkmark: test what it can carry.
        command = f"echo {MARKER_CMD}& exit 3"
    else:
        command = f"printf '%s\\n' '{MARKER}'; exit 3"
    result = execs.run(command, shell=shell, timeout_seconds=60)
    if result["timed_out"]:
        raise CheckFailed("timed out")
    if result["exit_code"] != 3:
        raise CheckFailed(f"exit code {result['exit_code']}, expected 3; stderr: {result['stderr'][:200]}")
    if result["stdout"].strip() != (MARKER_CMD if shell == "cmd" else MARKER):
        raise CheckFailed(f"UTF-8 round trip failed: {result['stdout'][:200]!r}")
    return f"exit code and UTF-8 ok in {result['duration_seconds']} s"


def _timeout_check() -> str:
    command = "Start-Sleep -Seconds 30" if IS_WINDOWS else "sleep 30"
    result = execs.run(command, timeout_seconds=1)
    if not result["timed_out"] or result["duration_seconds"] > 15:
        raise CheckFailed(f"timeout did not return promptly: {result['duration_seconds']} s")
    return f"returned after {result['duration_seconds']} s"


def _text_check(folder: Path) -> str:
    path = folder / "probe.xml"
    original = b"\xef\xbb\xbf<a>\r\n  <b>alt</b>\r\n</a>\r\n"
    path.write_bytes(original)
    read = textfiles.read_text(str(path))
    textfiles.patch_text(str(path), read["sha256"], [{"old_text": "alt", "new_text": "neu"}])
    if path.read_bytes() != original.replace(b"alt", b"neu"):
        raise CheckFailed("encoding, BOM or line endings changed")
    return "UTF-8 BOM and CRLF preserved"


def _job_check(state: Path) -> str:
    manager = jobs.Jobs(state)
    try:
        command = "Write-Output job-ok" if IS_WINDOWS else "echo job-ok"
        job = manager.start(command, timeout_seconds=120)
        status = manager.wait([job["job_id"]], 60)["jobs"][0]
        if status["state"] != "exited" or status["exit_code"] != 0:
            raise CheckFailed(f"job ended as {status['state']} with {status['exit_code']}")
        output = manager.output(job["job_id"])["content"].strip()
        if output != "job-ok":
            raise CheckFailed(f"unexpected output {output!r}")
        return f"started, waited, read in {status['duration_seconds']} s"
    finally:
        manager.shutdown()


def _long_paths() -> str:
    from .server import long_paths_enabled  # noqa: PLC0415

    enabled = long_paths_enabled()
    if enabled is False:
        raise CheckFailed("LongPathsEnabled is 0; an admin enables it once (deep D365 paths fail without)")
    return "enabled" if enabled else "not applicable"


def first_error(text: str) -> str:
    """The first readable error line. Redirected, Windows PowerShell writes its error stream as
    CLIXML ("#< CLIXML" then <Objs ...>): take the <S S="Error"> texts out of it."""
    if "#< CLIXML" in text or "<Objs " in text:
        parts = re.findall(r'<S S="Error">(.*?)</S>', text, flags=re.S)
        text = html.unescape("".join(parts))
        text = re.sub(r"_x([0-9A-Fa-f]{4})_", lambda m: chr(int(m.group(1), 16)), text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[0][:200] if lines else "failed"


def _powershell(command: str) -> str:
    result = execs.run(command, shell="powershell", timeout_seconds=60)
    if result["timed_out"] or result["exit_code"] != 0:
        raise CheckFailed(first_error(result["stderr"] or result["stdout"] or ""))
    return result["stdout"].strip()


# What the other servers need, checked without importing them: reported, never required --
# a box without D365 still runs the core.
def _d365fo_tools() -> str:
    version = _powershell("$m = Get-Module -ListAvailable d365fo.tools | Sort-Object Version -Descending | "
                          "Select-Object -First 1; if ($m) { $m.Version.ToString() }")
    if not version:
        raise CheckFailed("not installed: the d365 server needs it (Install-Module d365fo.tools)")
    return f"d365fo.tools {version}"


def _xref_database() -> str:
    version = _powershell(
        "$c = New-Object System.Data.SqlClient.SqlConnection "
        "'Server=localhost;Database=DYNAMICSXREFDB;Integrated Security=true;Connect Timeout=5'; "
        "$c.Open(); try { $q = $c.CreateCommand(); $q.CommandText = 'SELECT Version FROM DBVersion'; "
        "$q.ExecuteScalar() } finally { $c.Close() }")
    return f"DYNAMICSXREFDB {version} on localhost (Windows authentication)"


class CheckFailed(Exception):
    """A finding, said as it is: the report shows its text without an exception name."""


def _pythons() -> list[str]:
    """This release's interpreters: python.exe (services, index build) and pythonw.exe (the core)."""
    here = Path(sys.executable)
    return [str(here.with_name(name)) for name in ("python.exe", "pythonw.exe") if here.with_name(name).exists()] \
        or [sys.executable]


def _defender() -> str:
    wanted = _pythons()
    listed = ", ".join("'" + p.replace("'", "''") + "'" for p in wanted)
    state = _powershell(
        "$p = Get-MpPreference -ErrorAction Stop; $s = Get-MpComputerStatus -ErrorAction Stop; "
        f"$want = @({listed}); "
        "if (-not $s.RealTimeProtectionEnabled) { 'off' } "
        "elseif ($p.ExclusionProcess -contains 'N/A: Must be an administrator to view exclusions') { 'unknown' } "
        "else { ($want | Where-Object { $p.ExclusionProcess -notcontains $_ }) -join '|' }")
    if state in ("off", "unknown"):
        return {"off": "real-time protection is off",
                "unknown": "exclusions are visible to administrators only"}[state]
    if state:
        raise CheckFailed(f"real-time protection scans every read of {state.replace('|', ' and ')}: "
                          "indexing and search are much slower (INSTALL.md, step 2)")
    return "this release's python.exe and pythonw.exe are excluded"


def run_checks(state_dir: Path | None = None) -> dict[str, Any]:
    from boring_service_kit.tools import default_state_dir  # noqa: PLC0415

    state = state_dir or default_state_dir("boring-pondnt")
    checks: list[dict[str, Any]] = []

    def check(name: str, fn: Callable[[], str], required: bool = True) -> None:
        started = time.monotonic()
        try:
            detail, ok = fn(), True
        except CheckFailed as finding:  # an expected finding: its text is the message
            detail, ok = str(finding), False
        except Exception as exc:  # report everything; a self-check never crashes
            detail, ok = f"{type(exc).__name__}: {exc}", False
        checks.append({"name": name, "ok": ok, "required": required, "detail": detail,
                       "seconds": round(time.monotonic() - started, 2)})

    def state_writable() -> str:
        state.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", dir=state) as probe:
            probe.write("ok")
            probe.flush()
        return str(state)

    check("state_dir", state_writable)
    shells = ["sh"] if not IS_WINDOWS else ["powershell", "cmd"] + (["pwsh"] if execs.pwsh_path() else [])
    for shell in shells:
        check(f"shell_{shell}", lambda shell=shell: _shell_check(shell))
    if IS_WINDOWS and not execs.pwsh_path():
        checks.append({"name": "shell_pwsh", "ok": False, "required": False, "seconds": 0,
                       "detail": "PowerShell 7 not found; exec falls back to Windows PowerShell 5.1"})
    check("timeout", _timeout_check)
    with tempfile.TemporaryDirectory(prefix="boring-pondnt-self-check-") as folder:
        check("text_round_trip", lambda: _text_check(Path(folder)))
        check("job", lambda: _job_check(Path(folder) / "state"))
    check("long_paths", _long_paths, required=IS_WINDOWS)
    if IS_WINDOWS:
        check("d365fo_tools", _d365fo_tools, required=False)
        check("xref_database", _xref_database, required=False)
        check("defender", _defender, required=False)

    return {
        "ok": all(c["ok"] for c in checks if c["required"]),
        "boring-pondnt": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "default_shell": execs.default_shell(),
        "pwsh": execs.pwsh_path(),
        "checks": checks,
    }


def main(state_dir: Path | None = None) -> int:
    report = run_checks(state_dir)
    # Bytes, not text: stdout's own encoding may be a legacy code page (Windows, redirected).
    sys.stdout.buffer.write((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    sys.stdout.flush()
    return 0 if report["ok"] else 1
