import os
import time
from boring_pondnt.d365 import Jobs
from boring_pondnt import JOB_RETENTION_SECONDS as RETENTION_SECONDS

def test_only_expired_confirmed_managed_logs_are_pruned(tmp_path):
    other = tmp_path / "d365-jobs" / "other-build.log"
    other.parent.mkdir()
    other.write_text("retained historical diagnostics")
    managed = other.parent / "managed"
    managed.mkdir()
    old = managed / ("a" * 12)
    pending = managed / ("b" * 12)
    fresh = managed / ("c" * 12)
    for directory in (old, pending, fresh):
        directory.mkdir()
        (directory / "build.log").write_text("synthetic diagnostics")
    ended = old / "ended"
    ended.touch()
    stale = time.time() - RETENTION_SECONDS - 60
    os.utime(ended, (stale, stale))
    (fresh / "ended").touch()
    Jobs("unused", tmp_path)
    assert not old.exists()
    assert pending.exists() and fresh.exists()
    assert other.read_text() == "retained historical diagnostics"
