"""exec: one command, waited for, output bounded.

Output goes to temporary files, never pipes, so a chatty build cannot block
on a full pipe and memory stays flat. What comes back is the head and the
tail of each stream with a marker between: a build's first error and its
summary are both visible.

Shells: PowerShell 7 (`pwsh`) by default, preferably the one
shipped in the server's own folder, else one on PATH; Windows PowerShell 5.1
(`powershell`) when no pwsh exists, or on request (d365fo.tools needs 5.1;
the D365 service uses it); `cmd` on request. PowerShell gets the command as
`-EncodedCommand` (UTF-16 base64), so no quoting ever reaches the command
line. Off Windows (tests), `sh`.

A timeout kills the whole tree and is reported as data (`timed_out: true`),
the shape the Pond's exec uses. Cleanup that cannot be confirmed is data too
(`cleanup_confirmed: false` with an `error`), the shape jobs use.
"""

from __future__ import annotations

import base64
import codecs
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from . import disk
from boring_service_kit.outcomes import Failed, Refused
from boring_service_kit.process import IS_WINDOWS, Tree, settle

TIMEOUT_DEFAULT = 60.0
TIMEOUT_MAX = 600.0
HEAD_BYTES = 16 * 1024
TAIL_BYTES = 16 * 1024

SHELLS_WINDOWS = ("pwsh", "powershell", "cmd")
SHELLS_POSIX = ("sh",)

POWERSHELL_PRELUDE = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
    "$OutputEncoding = [System.Text.Encoding]::UTF8\n"
    "$ProgressPreference = 'SilentlyContinue'\n"
    "if (Test-Path variable:PSStyle) { $PSStyle.OutputRendering = 'PlainText' }\n"
)
# Keep a native program's exit code; a failed cmdlet as the last statement gives 1.
POWERSHELL_EPILOGUE = (
    "\n$__ok = $?\n"
    "if ($LASTEXITCODE -is [int] -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
    "if (-not $__ok) { exit 1 }\n"
    "exit 0\n"
)


def pwsh_path() -> str | None:
    """PowerShell 7: an explicit override, the bundled copy, or one on PATH."""
    override = os.environ.get("BORING_PONDNT_PWSH")
    if override:
        return override if Path(override).is_file() else None
    bundled = Path(sys.prefix).parent / "pwsh" / ("pwsh.exe" if IS_WINDOWS else "pwsh")
    if bundled.is_file():
        return str(bundled)
    return shutil.which("pwsh")


def default_shell() -> str:
    if not IS_WINDOWS:
        return "sh"
    return "pwsh" if pwsh_path() else "powershell"


def build_argv(shell: str, command: str) -> list[str] | str:
    if shell in ("pwsh", "powershell"):
        script = POWERSHELL_PRELUDE + command + POWERSHELL_EPILOGUE
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        executable = (pwsh_path() or "pwsh") if shell == "pwsh" else shell
        return [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-EncodedCommand", encoded]
    if shell == "cmd":
        # One string: cmd parses its own command line; /s keeps the quotes as written.
        # No `chcp 65001`: probed on Windows Server 2022, it does not change the
        # encoding of an `echo` later on the same line; output is decoded instead (decode_output).
        return f'cmd.exe /d /s /c "{command}"'
    if shell == "sh":
        return ["sh", "-c", command]
    raise Refused("invalid_request", f"unknown shell {shell!r}")


def environment() -> dict[str, str]:
    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "DOTNET_CLI_UI_LANGUAGE": "en",
                "NO_COLOR": "1", "TERM": "dumb",
                "POWERSHELL_TELEMETRY_OPTOUT": "1", "POWERSHELL_UPDATECHECK": "Off"})
    return env


def working_directory(cwd: str | None) -> Path:
    if cwd is None:
        return Path.home()
    p = disk.absolute(cwd)
    if not p.is_dir():
        raise Refused("not_found", "working directory does not exist", path=str(p))
    return p


def oem_code_page() -> str | None:
    """The console's legacy code page on Windows (e.g. cp437), None elsewhere."""
    if not IS_WINDOWS:
        return None
    import ctypes  # noqa: PLC0415

    return f"cp{ctypes.windll.kernel32.GetOEMCP()}"


def decode_output(data: bytes, fallback: str | None) -> tuple[str, str]:
    """(text, encoding): UTF-8 when the bytes are valid UTF-8, else the fallback code page.

    cmd's built-ins (echo, dir, set) write in the console's OEM code page -- cp437 on the
    probed Windows Server 2022 -- while PowerShell (our prelude), Python (PYTHONUTF8) and
    most modern tools write UTF-8. Valid UTF-8 is almost never accidental in legacy text,
    so this tells them apart without knowing the program.
    """
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        encoding = _output_encoding(False, fallback)
        return data.decode(encoding.split()[0], errors="replace"), encoding


def _output_encoding(valid_utf8: bool, fallback: str | None) -> str:
    return "utf-8" if valid_utf8 else (fallback or "utf-8 (with replacements)")


def line_window(path: Path, start_line: int | None, count: int,
                max_chars: int, fallback: str | None) -> dict[str, Any]:
    """Read a line window with bounded memory, from the initial byte extent.

    Counting all lines and choosing UTF-8 versus the OEM fallback requires a
    scan. A second bounded scan selects the window; appends cannot extend it.
    No readline() call may allocate an arbitrarily long build-output line.
    """
    with path.open("rb") as handle:
        size = os.fstat(handle.fileno()).st_size

        def chunks():
            handle.seek(0)
            remaining = size
            while remaining:
                block = handle.read(min(65536, remaining))
                if not block:
                    break
                remaining -= len(block)
                yield block

        validator = codecs.getincrementaldecoder("utf-8")()
        valid_utf8, total, last = True, 0, b""
        for block in chunks():
            total += block.count(b"\n")
            last = block[-1:]
            if valid_utf8:
                try:
                    validator.decode(block)
                except UnicodeDecodeError:
                    valid_utf8 = False
        if valid_utf8:
            try:
                validator.decode(b"", final=True)
            except UnicodeDecodeError:
                valid_utf8 = False
        if last and last != b"\n":
            total += 1
        encoding = _output_encoding(valid_utf8, fallback)
        first = max(0, total - count) if start_line is None else min(start_line - 1, total)
        returned = min(count, total - first)
        decoder = codecs.getincrementaldecoder(encoding.split()[0])(errors="replace")
        content, selected_chars, line = "", 0, 0

        def consume(text):
            nonlocal content, selected_chars, line
            parts = text.split("\n")
            for index, part in enumerate(parts):
                newline = index < len(parts) - 1
                if first <= line < first + returned:
                    piece = part + ("\n" if newline else "")
                    selected_chars += len(piece)
                    content = ((content + piece)[-max_chars:] if start_line is None
                               else (content + piece)[:max_chars])
                if newline:
                    line += 1

        for block in chunks():
            consume(decoder.decode(block))
        consume(decoder.decode(b"", final=True))
        if last and last != b"\n":
            consume("\n")  # output's established contract terminates a partial final line
    return {"content": content, "start_line": first + 1, "returned_lines": returned,
            "total_lines": total, "content_cut": selected_chars > max_chars,
            **({"encoding": encoding} if encoding != "utf-8" else {})}


def _utf8_parts(first: bytes, last: bytes) -> tuple[str, int, str, int] | None:
    """(head, head bytes cut, tail, tail bytes cut) if both parts are UTF-8 apart from the cuts.

    The head may end inside a character and the tail may start inside one; those
    partial characters are dropped. Anything else that is not UTF-8 gives None.
    """
    skip = 0
    while skip < min(3, len(last)) and 0x80 <= last[skip] <= 0xBF:
        skip += 1
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        head = decoder.decode(first)
        tail = last[skip:].decode("utf-8")
    except UnicodeDecodeError:
        return None
    pending = len(decoder.getstate()[0])
    return head, pending, tail, skip


def bounded(path: Path, head: int = HEAD_BYTES, tail: int = TAIL_BYTES,
            fallback: str | None = None) -> tuple[str, bool, int, str]:
    """(text, truncated, total bytes, encoding): the whole file, or its head and tail with a marker."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size <= head + tail:
            text, encoding = decode_output(handle.read(), fallback)
            return text, False, size, encoding
        first = handle.read(head)
        handle.seek(size - tail)
        last = handle.read(tail)
    # Each part is judged on its own: joining them would glue the head's last
    # character to the middle of another one and make valid UTF-8 look like OEM text.
    parts = _utf8_parts(first, last)
    if parts is not None:
        head_text, head_cut, tail_text, tail_cut = parts
        omitted = size - (len(first) - head_cut) - (len(last) - tail_cut)
        return (head_text + f"\n[... {omitted} bytes omitted ...]\n" + tail_text,
                True, size, "utf-8")
    encoding = _output_encoding(False, fallback)
    codec = encoding.split()[0]
    text = (first.decode(codec, errors="replace")
            + f"\n[... {size - head - tail} bytes omitted ...]\n"
            + last.decode(codec, errors="replace"))
    return text, True, size, encoding


def check_shell(shell: str | None) -> str:
    chosen = shell or default_shell()
    allowed = SHELLS_WINDOWS if IS_WINDOWS else SHELLS_POSIX
    if chosen not in allowed:
        raise Refused("invalid_request", f"shell must be one of {', '.join(allowed)}")
    return chosen


def check_command(command: str, shell: str | None, timeout_seconds: float,
                  maximum: float = TIMEOUT_MAX) -> str:
    """Stateless command checks shared by execution, jobs and batch admission."""
    if not command.strip():
        raise Refused("invalid_request", "command must be nonempty")
    if not 0.1 <= timeout_seconds <= maximum:
        raise Refused("invalid_request", f"timeout_seconds must be 0.1-{maximum:g}")
    return check_shell(shell)


def run(command: str, shell: str | None = None, cwd: str | None = None,
        timeout_seconds: float = TIMEOUT_DEFAULT) -> dict[str, Any]:
    chosen = check_command(command, shell, timeout_seconds)
    workdir = working_directory(cwd)
    argv = build_argv(chosen, command)
    # A tree whose cleanup is unconfirmed may still hold the output files open;
    # a leftover temporary folder is then the lesser harm than a lost result.
    with tempfile.TemporaryDirectory(prefix="boring-pondnt-exec-", ignore_cleanup_errors=True) as scratch:
        out_path, err_path = Path(scratch) / "stdout", Path(scratch) / "stderr"
        with out_path.open("wb") as out, err_path.open("wb") as err:
            started = time.monotonic()
            try:
                tree = Tree(argv, workdir, environment(), out, err)
            except FileNotFoundError:
                raise Refused("not_found", f"shell {chosen!r} is not installed") from None
            except OSError as exc:
                raise Failed("start_failed", str(exc)) from None
            # settle() owns wait, kill and close, and reports cleanup failures as data.
            settlement = settle(tree, threading.Event(), timeout_seconds)
            duration = time.monotonic() - started
        fallback = oem_code_page()
        stdout, stdout_truncated, stdout_bytes, stdout_encoding = bounded(out_path, fallback=fallback)
        stderr, stderr_truncated, stderr_bytes, stderr_encoding = bounded(err_path, fallback=fallback)
    result: dict[str, Any] = {
        "exit_code": settlement.exit_code,
        "timed_out": settlement.state == "timed_out",
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "duration_seconds": round(duration, 3),
        "shell": chosen,
        "cwd": str(workdir),
    }
    if settlement.error is not None:
        result["error"] = settlement.error
    if settlement.cleanup_error is not None:
        result["cleanup_confirmed"] = False
        result["error"] = settlement.cleanup_error
    # Said only when it was not UTF-8, so the ordinary result stays as small as before.
    if stdout_encoding != "utf-8":
        result["stdout_encoding"] = stdout_encoding
    if stderr_encoding != "utf-8":
        result["stderr_encoding"] = stderr_encoding
    return result
