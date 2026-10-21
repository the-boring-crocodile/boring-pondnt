"""Core job admission and shutdown races; injected trees, no live commands."""
import threading
import sys

import pytest

from boring_pondnt import jobs
from boring_service_kit.outcomes import Failed


class Tree:
    def __init__(self, *args):
        self.closed = threading.Event()
    def wait(self, timeout):
        self.closed.wait(timeout)
        return None
    def close(self):
        self.closed.set()


def test_shutdown_refuses_new_jobs_before_any_files_or_processes(tmp_path, monkeypatch):
    def forbidden(*args):
        pytest.fail("closed manager spawned a process")
    monkeypatch.setattr(jobs, "Tree", forbidden)
    manager = jobs.Jobs(tmp_path)
    manager.shutdown()
    before = list(manager.root.iterdir())
    with pytest.raises(Failed) as error:
        manager.start("fixture", shell="cmd" if sys.platform == "win32" else "sh")
    assert error.value.code == "unavailable"
    assert list(manager.root.iterdir()) == before


def test_shutdown_owns_a_concurrently_starting_job(tmp_path, monkeypatch):
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    tree = Tree()
    def delayed(*args):
        entered.set()
        assert release.wait(5)
        return tree
    monkeypatch.setattr(jobs, "Tree", delayed)
    manager = jobs.Jobs(tmp_path)
    started, errors = [], []
    def start():
        try:
            started.append(manager.start("fixture", shell="cmd" if sys.platform == "win32" else "sh"))
        except BaseException as error:
            errors.append(error)
    def shutdown():
        try:
            manager.shutdown()
        finally:
            closed.set()
    worker = threading.Thread(target=start)
    closer = threading.Thread(target=shutdown)
    worker.start()
    try:
        assert entered.wait(3)
        closer.start()
        # Old code finishes shutdown while Tree is still unregistered.
        closed.wait(0.2)
        release.set()
        worker.join(5)
        closer.join(5)
        assert not worker.is_alive() and not closer.is_alive()
        assert not errors and len(started) == 1
        assert tree.closed.is_set(), "shutdown missed the admitted process"
        assert manager.status(started[0]["job_id"])["state"] == "stopped"
    finally:
        release.set()
        worker.join(5)
        manager.shutdown()
        if closer.ident is not None:
            closer.join(5)


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_failed_watcher_start_keeps_and_settles_process_ownership(tmp_path, monkeypatch, cleanup_fails):
    tree = Tree()
    if cleanup_fails:
        def fail():
            raise OSError("synthetic private detail")
        tree.close = fail
    monkeypatch.setattr(jobs, "Tree", lambda *args: tree)
    original = threading.Thread.start
    def thread_start(self):
        if self.name.startswith("job-"):
            raise RuntimeError("synthetic private detail")
        return original(self)
    monkeypatch.setattr(threading.Thread, "start", thread_start)
    manager = jobs.Jobs(tmp_path)
    try:
        with pytest.raises(Failed) as error:
            manager.start("fixture", shell="cmd" if sys.platform == "win32" else "sh")
        assert error.value.code == "start_failed"
        job = manager._get(error.value.details["job_id"])
        assert job.done.is_set()
        assert job.state == "failed"
        assert job.tree is tree
        assert "private detail" not in str(job.status())
        if cleanup_fails:
            assert job.status()["cleanup_confirmed"] is False
        else:
            assert tree.closed.is_set()
    finally:
        manager.shutdown()
