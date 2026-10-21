"""exec: exit codes, UTF-8, bounded output, and trees that die with their root."""

from __future__ import annotations

import errno
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

from boring_pondnt import execs
from boring_service_kit.outcomes import Refused

posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell semantics")
windows = pytest.mark.skipif(sys.platform != "win32", reason="needs real Windows")


def _proc_stat(pid: int) -> list[str] | None:
    """Fields of /proc/<pid>/stat after the command name (state is [0], start time [19])."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        # Gone before the open, or between the open and the read (ESRCH).
        return None
    return raw.rsplit(")", 1)[1].split()


def alive(pid: int, start_time: str | None = None) -> bool:
    if sys.platform == "win32":
        out = execs.run(f'tasklist /FI "PID eq {pid}" /NH', shell="cmd")["stdout"]
        return str(pid) in out
    fields = _proc_stat(pid)
    if fields is None or fields[0] == "Z":  # gone, or a zombie: dead for our purposes
        return False
    # Same PID, different start time: the PID was reused by another process.
    return start_time is None or fields[19] == start_time


def wait_dead(pid: int, seconds: float = 5.0, start_time: str | None = None) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not alive(pid, start_time):
            return True
        time.sleep(0.05)
    return False


def pid_and_start(pidfile: Path) -> tuple[int, str | None]:
    """`<pid> <start time>` as written by RECORD_CHILD, or a bare PID."""
    parts = pidfile.read_text().split()
    return int(parts[0]), (parts[1] if len(parts) > 1 else None)


def explain(pid: int) -> str:
    fields = _proc_stat(pid)
    return "gone" if fields is None else f"state {fields[0]}, start time {fields[19]}"


# Writes the background child's PID and its start time (field 22 of /proc/<pid>/stat).
RECORD_CHILD = "echo $! $(cut -d')' -f2- /proc/$!/stat | cut -d' ' -f21)"


@posix
def test_stdout_stderr_exit_code(tmp_path):
    result = execs.run("echo out; echo err >&2; exit 3", cwd=str(tmp_path))
    assert result["stdout"] == "out\n" and result["stderr"] == "err\n"
    assert result["exit_code"] == 3 and not result["timed_out"]
    assert result["shell"] == "sh" and result["cwd"] == str(tmp_path)


@posix
def test_utf8_output():
    assert execs.run("printf 'Grüße äöü\\n'")["stdout"] == "Grüße äöü\n"


@posix
def test_default_cwd_is_home():
    assert execs.run("pwd")["stdout"].strip() == str(Path.home())


@posix
def test_large_output_keeps_head_and_tail():
    result = execs.run("i=0; while [ $i -lt 20000 ]; do echo line$i; i=$((i+1)); done")
    assert result["stdout_truncated"] and result["stdout_bytes"] > 100_000
    assert result["stdout"].startswith("line0\nline1\n")
    assert result["stdout"].rstrip().endswith("line19999")
    assert "bytes omitted" in result["stdout"]


@posix
def test_timeout_kills_the_whole_tree(tmp_path):
    pidfile = tmp_path / "child.pid"
    result = execs.run(f"sleep 30 & {RECORD_CHILD} > {pidfile}; sleep 30", timeout_seconds=1)
    assert result["timed_out"] and result["exit_code"] is None
    assert result["duration_seconds"] < 10
    pid, start = pid_and_start(pidfile)
    assert wait_dead(pid, start_time=start), explain(pid)


@posix
def test_background_children_die_with_a_finished_command(tmp_path):
    pidfile = tmp_path / "child.pid"
    result = execs.run(f"sleep 30 > /dev/null 2>&1 & {RECORD_CHILD} > {pidfile}; exit 0")
    assert result["exit_code"] == 0
    pid, start = pid_and_start(pidfile)
    assert wait_dead(pid, start_time=start), explain(pid)


@pytest.mark.parametrize(
    "kwargs, code",
    [
        ({"command": "  "}, "invalid_request"),
        ({"command": "true", "timeout_seconds": 0}, "invalid_request"),
        ({"command": "true", "timeout_seconds": 601}, "invalid_request"),
        ({"command": "true", "shell": "bash-please"}, "invalid_request"),
        ({"command": "true", "cwd": "relative"}, "invalid_request"),
    ],
)
def test_refusals(kwargs, code):
    with pytest.raises(Refused) as info:
        execs.run(**kwargs)
    assert info.value.code == code


def test_missing_cwd_is_not_found(tmp_path):
    with pytest.raises(Refused) as info:
        execs.run("true", cwd=str(tmp_path / "missing"))
    assert info.value.code == "not_found"


def test_bounded_reads_whole_small_files(tmp_path):
    path = tmp_path / "out"
    path.write_bytes("ä".encode() * 10)
    assert execs.bounded(path) == ("ä" * 10, False, 20, "utf-8")


@pytest.mark.parametrize("head", [4, 5])
@pytest.mark.parametrize("count", [50, 51])
def test_truncated_utf8_stays_utf8_when_a_seam_cuts_a_character(tmp_path, head, count):
    # 'x' then two-byte characters: odd offsets start a character, even ones are inside it.
    data = ("x" + "ä" * count).encode()
    path = tmp_path / "out"
    path.write_bytes(data)
    text, truncated, size, encoding = execs.bounded(path, head=head, tail=5, fallback="cp437")
    assert (truncated, size, encoding) == (True, len(data), "utf-8")
    first, marker, last = text.partition("\n[... ")
    omitted = int(marker and last.split(" ", 1)[0])
    last = last.split(" ...]\n", 1)[1]
    assert "�" not in text
    assert first == "x" + "ä" * ((head - 1) // 2)
    assert set(last) == {"ä"}
    assert len(first.encode()) + omitted + len(last.encode()) == len(data)


def test_truncated_oem_output_keeps_the_fallback(tmp_path):
    path = tmp_path / "out"
    path.write_bytes(bytes.fromhex("47 72 81 e1 65 0d 0a") * 20)  # cmd's cp437 "Grüße"
    text, truncated, _, encoding = execs.bounded(path, head=14, tail=14, fallback="cp437")
    assert truncated and encoding == "cp437"
    assert text.startswith("Grüße\r\nGrüße\r\n") and text.endswith("Grüße\r\nGrüße\r\n")


class UnconfirmedTree:
    """A command that exited, but whose tree could not be confirmed dead."""

    def __init__(self, *args):
        pass

    def wait(self, timeout):
        return 0

    def close(self):
        raise TimeoutError("synthetic private detail")


def test_unconfirmed_cleanup_is_reported_as_data(monkeypatch):
    monkeypatch.setattr(execs, "Tree", UnconfirmedTree)
    result = execs.run("fixture", shell=execs.default_shell())
    assert result["cleanup_confirmed"] is False
    assert result["exit_code"] is None and result["timed_out"] is False
    assert result["error"] == "TimeoutError: cleanup unconfirmed"
    assert "private detail" not in str(result)


def test_temporary_folder_cleanup_errors_do_not_escape(tmp_path, monkeypatch):
    # The folder cannot be removed, as when a surviving process still holds a file open.
    def busy(path, ignore_errors=False, onerror=None, *, onexc=None, dir_fd=None):
        error = OSError(errno.EBUSY, "output file still open", str(path))
        if onexc is not None:
            onexc(os.rmdir, path, error)
        elif onerror is not None:
            onerror(os.rmdir, path, (OSError, error, None))
        else:
            raise error

    monkeypatch.setattr(execs.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(shutil, "rmtree", busy)
    result = execs.run("echo ok")
    assert result["exit_code"] == 0 and result["stdout"].strip() == "ok"


def test_output_decoding_prefers_utf8_and_falls_back_to_the_oem_code_page():
    # The bytes cmd's echo wrote on the probed Windows Server 2022 (OEM code page 437).
    cp437 = bytes.fromhex("47 72 81 e1 65 20 84 94 81 0d 0a")
    assert execs.decode_output(cp437, "cp437") == ("Grüße äöü\r\n", "cp437")
    assert execs.decode_output("Grüße".encode(), "cp437") == ("Grüße", "utf-8")
    text, encoding = execs.decode_output(cp437, None)
    assert encoding == "utf-8 (with replacements)" and "\ufffd" in text


def test_cmd_argv_has_no_ineffective_chcp():
    assert "chcp" not in execs.build_argv("cmd", "echo x")


def test_pwsh_resolution_prefers_the_override(tmp_path, monkeypatch):
    fake = tmp_path / "pwsh.exe"
    fake.write_text("")
    monkeypatch.setenv("BORING_PONDNT_PWSH", str(fake))
    assert execs.pwsh_path() == str(fake)
    assert execs.build_argv("pwsh", "Write-Output x")[0] == str(fake)
    monkeypatch.setenv("BORING_PONDNT_PWSH", str(tmp_path / "missing.exe"))
    assert execs.pwsh_path() is None


def test_powershell_argv_is_encoded_and_quote_free():
    argv = execs.build_argv("powershell", 'Write-Output "a b" ; exit 3')
    assert argv[:2] == ["powershell", "-NoLogo"] and argv[-2] == "-EncodedCommand"
    import base64
    script = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert 'Write-Output "a b" ; exit 3' in script and "OutputEncoding" in script
    assert "PSStyle" in script  # plain output on PowerShell 7, harmless on 5.1


# --- Windows only: these run on the Windows CI runner and the VM -------------


@windows
@pytest.mark.parametrize("shell", ["powershell", None])  # 5.1 and the default (pwsh if present)
def test_windows_powershell_exit_codes(shell):
    assert execs.run("cmd /c exit 7", shell=shell)["exit_code"] == 7
    assert execs.run("Get-Item C:\\does-not-exist-boring", shell=shell)["exit_code"] == 1
    assert execs.run("Write-Output ok", shell=shell)["exit_code"] == 0


@windows
def test_windows_utf8_both_ways():
    result = execs.run("Write-Output 'Grüße äöü'")
    assert result["stdout"].strip() == "Grüße äöü"
    result = execs.run("echo Grüße äöü", shell="cmd")
    assert result["stdout"].strip() == "Grüße äöü"  # cmd's echo: OEM code page, decoded as such


@windows
def test_windows_cmd_quoting():
    result = execs.run('echo "a  b"', shell="cmd")
    assert result["stdout"].strip() == '"a  b"'
    assert execs.run("exit 4", shell="cmd")["exit_code"] == 4


@windows
def test_windows_timeout_kills_children(tmp_path):
    pidfile = tmp_path / "child.pid"
    command = (
        "$p = Start-Process -FilePath ping -ArgumentList '-n','60','127.0.0.1' "
        "-WindowStyle Hidden -PassThru; "
        f"Set-Content -Path '{pidfile}' -Value $p.Id; Start-Sleep -Seconds 60"
    )
    result = execs.run(command, timeout_seconds=3)
    assert result["timed_out"]
    assert wait_dead(int(pidfile.read_text().strip()), 10)


@windows
def test_windows_children_die_when_the_command_ends(tmp_path):
    pidfile = tmp_path / "child.pid"
    command = (
        "$p = Start-Process -FilePath ping -ArgumentList '-n','60','127.0.0.1' "
        "-WindowStyle Hidden -PassThru; "
        f"Set-Content -Path '{pidfile}' -Value $p.Id"
    )
    assert execs.run(command)["exit_code"] == 0
    assert wait_dead(int(pidfile.read_text().strip()), 10)


def test_windows_creation_flags_are_defined_everywhere():
    """Regression: subprocess has no CREATE_SUSPENDED; the flags are ours, checkable on any platform."""
    from boring_service_kit import process

    assert process.CREATE_SUSPENDED == 0x4
    assert process.CREATE_NEW_PROCESS_GROUP == 0x200
    assert process.CREATE_NO_WINDOW == 0x08000000
