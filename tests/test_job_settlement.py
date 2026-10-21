"""Cleanup ownership is observable before terminal state; no real Win32 process."""
import threading

import pytest
from boring_pondnt import jobs
from test_process_start_cleanup import windows, start  # noqa: F401  (pytest fixture)


@pytest.mark.parametrize("phase", ["terminate", "query", "close"])
def test_windows_cleanup_failure_is_not_swallowed(windows, phase):  # noqa: F811
    tree = start()
    failing = {"terminate": windows.jobs.TerminateJobObject,
               "query": windows.jobs.QueryInformationJobObject,
               "close": windows.api.CloseHandle}[phase]
    failing.side_effect = OSError("synthetic cleanup failure")
    with pytest.raises(OSError, match="synthetic cleanup"):
        tree.close()
    assert tree._job is windows.job


def test_windows_waits_for_active_descendants_before_releasing_handle(windows, monkeypatch):  # noqa: F811
    from boring_service_kit import process
    tree = start()
    windows.jobs.QueryInformationJobObject.side_effect = [
        {"ActiveProcesses": 1}, {"ActiveProcesses": 0}]
    def pause(seconds):
        windows.api.CloseHandle.assert_not_called()
    monkeypatch.setattr(process.time, "sleep", pause)
    tree.close()
    windows.api.CloseHandle.assert_called_once_with(windows.job)


def test_unknown_population_does_not_release_handle(windows):  # noqa: F811
    tree = start()
    windows.jobs.QueryInformationJobObject.return_value = {"ActiveProcesses": True}
    with pytest.raises(OSError, match="invalid Job Object"):
        tree.close()
    windows.api.CloseHandle.assert_not_called()


def test_job_is_not_terminal_until_cleanup_finishes(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Tree:
        def wait(self, timeout):
            return None
        def close(self):
            entered.set()
            assert release.wait(3)
    manager = jobs.Jobs(tmp_path)
    directory = manager.root / "synthetic"
    directory.mkdir()
    job = jobs.Job("synthetic", "fixture", "sh", str(tmp_path), 10, directory, 0, tree=Tree())
    manager._jobs[job.job_id] = job
    manager._request_stop(job)
    watcher = threading.Thread(target=manager._watch, args=(job,))
    watcher.start()
    try:
        assert entered.wait(2)
        assert manager.status(job.job_id)["state"] == "stopping"
        assert not manager.wait([job.job_id], 0)["all_ended"]
        release.set()
        watcher.join(3)
        assert not watcher.is_alive()
        assert job.state == "stopped" and job.done.is_set()
    finally:
        release.set()
        watcher.join(3)
        manager.shutdown()


def test_cleanup_failure_is_recorded_and_waiter_released(tmp_path):
    class Tree:
        def wait(self, timeout):
            return 0
        def close(self):
            raise OSError("synthetic private detail")
    manager = jobs.Jobs(tmp_path)
    directory = manager.root / "synthetic"
    directory.mkdir()
    job = jobs.Job("synthetic", "fixture", "sh", str(tmp_path), 10, directory, 0, tree=Tree())
    manager._jobs[job.job_id] = job
    manager._watch(job)
    assert job.done.is_set()
    status = manager.status(job.job_id)
    assert status["state"] == "failed"
    assert status["cleanup_confirmed"] is False
    assert "private detail" not in status["error"]
    manager.shutdown()


def test_wait_failure_still_runs_cleanup_once(tmp_path):
    from unittest.mock import Mock
    tree = Mock()
    tree.wait.side_effect = OSError("synthetic wait failure")
    manager = jobs.Jobs(tmp_path)
    directory = manager.root / "synthetic"
    directory.mkdir()
    job = jobs.Job("synthetic", "fixture", "sh", str(tmp_path), 10, directory, 0, tree=tree)
    manager._jobs[job.job_id] = job
    manager._watch(job)
    tree.close.assert_called_once_with()
    assert job.done.is_set() and job.state == "failed"
    assert job.status()["error"] == "OSError: execution wait failed"
    assert "cleanup_confirmed" not in job.status()
    manager.shutdown()


def test_windows_lingering_population_is_unconfirmed(windows, monkeypatch):  # noqa: F811
    from boring_service_kit import process
    tree = start()
    windows.jobs.QueryInformationJobObject.return_value = {"ActiveProcesses": 1}
    clock = iter([0, 11])
    monkeypatch.setattr(process.time, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError, match="cleanup unconfirmed"):
        tree.close()
    windows.api.CloseHandle.assert_not_called()
