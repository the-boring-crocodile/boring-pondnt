"""Native Job Object acceptance; only synthetic children and temporary state."""
import sys
import time

import pytest

from boring_pondnt.jobs import Jobs
from test_execs import wait_dead

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native Windows job lifecycle")


@pytest.mark.parametrize("ending,expected", [
    ("root_exit", "exited"), ("timeout", "timed_out"),
    ("stop", "stopped"), ("shutdown", "stopped"),
])
def test_terminal_job_has_no_surviving_child(tmp_path, ending, expected):
    manager = Jobs(tmp_path / "state")
    pidfile = tmp_path / "child.pid"
    quoted_path = str(pidfile).replace("'", "''")
    command = (
        "$p = Start-Process -FilePath ping -ArgumentList '-n','60','127.0.0.1' "
        "-WindowStyle Hidden -PassThru; "
        f"[IO.File]::WriteAllText('{quoted_path}', [string]$p.Id); "
        + ("" if ending == "root_exit" else "Start-Sleep -Seconds 60")
    )
    try:
        started = manager.start(command, shell="powershell",
                                timeout_seconds=15 if ending == "timeout" else 30)
        job_id = started["job_id"]
        deadline = time.monotonic() + 12
        pid = None
        while time.monotonic() < deadline:
            try:
                pid = int(pidfile.read_text().strip())
                break
            except (FileNotFoundError, ValueError):
                time.sleep(0.05)
        assert pid is not None, "synthetic child did not report readiness"
        if ending == "stop":
            manager.stop(job_id)
        elif ending == "shutdown":
            manager.shutdown()
        result = manager.wait([job_id], 30)
        assert result["all_ended"]
        status = result["jobs"][0]
        assert status["state"] == expected, status
        assert status.get("cleanup_confirmed") is not False, status
        assert wait_dead(pid, 10), "terminal job left its synthetic child alive"
    finally:
        manager.shutdown()
