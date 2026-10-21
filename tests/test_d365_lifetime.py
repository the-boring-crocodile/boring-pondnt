"""D365 admission, settlement and shutdown; only synthetic processes/data."""
import asyncio
import os
import sys
import threading
import time
from unittest.mock import Mock

import pytest

from boring_pondnt import d365
from boring_pondnt.d365_server import create_server
from boring_service_kit.outcomes import Failed
from test_d365 import fake_ps  # noqa: F401  (pytest fixture)
from test_execs import wait_dead


def wait_file(path):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            value = path.read_text().strip()
            if value:
                return value
        except FileNotFoundError:
            pass
        time.sleep(0.02)
    pytest.fail("synthetic process did not report readiness")


@pytest.mark.parametrize("ending", ["root_exit", "stop", "shutdown"])
def test_job_settlement_removes_descendants(fake_ps, tmp_path, monkeypatch, ending):  # noqa: F811
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_PS_CHILD_PID_FILE", str(pidfile))
    jobs = d365.Jobs(fake_ps, tmp_path)
    try:
        job = jobs.start("fixture", "normal" if ending == "root_exit" else "SlowJob")
        pid = int(wait_file(pidfile))
        if ending == "stop":
            jobs.stop(job["job_id"])
        if ending == "shutdown":
            jobs.shutdown()
        result = jobs.wait(job["job_id"], 15)
        assert not result["wait_expired"]
        assert result["state"] == ("exited" if ending == "root_exit" else "stopped")
        assert result.get("cleanup_confirmed") is not False
        assert wait_dead(pid, 10)
    finally:
        jobs.shutdown()
    with pytest.raises(Failed, match="closed"):
        jobs.start("fixture", "must not run")


def test_close_during_call_kills_child_without_restarting(fake_ps, tmp_path, monkeypatch):  # noqa: F811
    marker, pidfile = tmp_path / "entered", tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_PS_CALL_MARKER", str(marker))
    monkeypatch.setenv("FAKE_PS_CHILD_PID_FILE", str(pidfile))
    session = d365.Session(fake_ps)
    session.start()
    failures = []
    def call():
        try:
            session.call("Sleep-Forever", timeout=30)
        except Failed as error:
            failures.append(error.code)
    caller = threading.Thread(target=call)
    try:
        session.ensure_ready(15)
        caller.start()
        wait_file(marker)
        pid = int(wait_file(pidfile))
        session.close()
        caller.join(5)
        assert not caller.is_alive()
        assert failures == ["unavailable"]
        assert session.state == {"state": "closed", "cleanup_confirmed": True}
        assert session._tree is None
        assert wait_dead(pid, 10)
        with pytest.raises(Failed, match="closed"):
            session.start()
    finally:
        session.close()
        if caller.ident is not None:
            caller.join(5)


def test_close_before_boot_admission_starts_nothing(monkeypatch):
    tree = Mock()
    monkeypatch.setattr(d365, "Tree", tree)
    session = d365.Session("unused")
    # Hold the admission lock until close has invalidated the scheduled boot.
    with session._lifecycle:
        session.start()
        session.close_admission()
    session.close()
    tree.assert_not_called()
    assert session.state["state"] == "closed"


def test_close_racing_process_creation_owns_the_published_tree(fake_ps, monkeypatch):  # noqa: F811
    entered, release = threading.Event(), threading.Event()
    actual = d365.Tree
    trees = []
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        tree = actual(*args, **kwargs)
        trees.append(tree)
        return tree
    monkeypatch.setattr(d365, "Tree", slow)
    session = d365.Session(fake_ps)
    session.start()
    closer = threading.Thread(target=session.close)
    try:
        assert entered.wait(3)
        closer.start()
        release.set()
        closer.join(10)
        assert not closer.is_alive()
        assert len(trees) == 1 and trees[0]._closed
        assert session.state["state"] == "closed"
    finally:
        release.set()
        session.close()
        if closer.ident is not None:
            closer.join(5)


def test_failed_session_cleanup_retains_ownership_and_blocks_restart(fake_ps):  # noqa: F811
    session = d365.Session(fake_ps)
    session.start()
    session.ensure_ready(15)
    tree = session._tree
    original = tree.close
    tree.close = Mock(side_effect=OSError("synthetic private detail"))
    try:
        with pytest.raises(Failed) as error:
            session.close()
        assert error.value.code == "cleanup_unconfirmed"
        assert session._tree is tree and session.state["cleanup_confirmed"] is False
        assert "private detail" not in str(session.state)
        with pytest.raises(Failed, match="closed"):
            session.start()
    finally:
        tree.close = original
        session.close()


def test_job_does_not_publish_exit_while_cleanup_is_pending(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Tree:
        def wait(self, timeout):
            return 0
        def close(self):
            entered.set()
            assert release.wait(5)
    jobs = d365.Jobs("unused", tmp_path)
    job = dict(job_id="j", kind="fixture", log=str(tmp_path / "build.log"), started=time.time(),
               tree=Tree(), state="running", exit_code=None,
               stop=threading.Event(), done=threading.Event())
    jobs._jobs["j"] = job
    watcher = threading.Thread(target=jobs._watch, args=(job,))
    watcher.start()
    try:
        assert entered.wait(3)
        assert jobs.wait("j", 0)["wait_expired"]
        assert jobs.status("j")["state"] == "running"
        release.set()
        watcher.join(5)
        assert jobs.wait("j", 0)["state"] == "exited"
    finally:
        release.set()
        watcher.join(5)
        jobs.shutdown()


def test_failed_job_cleanup_is_never_stopped(tmp_path):
    tree = Mock()
    tree.close.side_effect = OSError("synthetic private detail")
    jobs = d365.Jobs("unused", tmp_path)
    job = dict(job_id="j", kind="fixture", log=str(tmp_path / "build.log"), started=time.time(),
               tree=tree, state="running", exit_code=None,
               stop=threading.Event(), done=threading.Event())
    jobs._jobs["j"] = job
    jobs._request_stop(job)
    jobs._watch(job)
    result = jobs.wait("j", 0)
    assert result["state"] == "failed" and result["cleanup_confirmed"] is False
    assert not result["wait_expired"]
    assert "private detail" not in str(result)
    tree.close.assert_called_once()
    with pytest.raises(Failed) as error:
        jobs.shutdown()
    assert error.value.code == "cleanup_unconfirmed"


def test_http_lifespan_shuts_down_session_and_jobs(fake_ps, tmp_path):  # noqa: F811
    server = create_server(fake_ps, tmp_path)
    server._d365_session.ensure_ready(15)
    session_tree = server._d365_session._tree
    async def scenario():
        app = server.streamable_http_app()
        async with app.router.lifespan_context(app):
            job = server._d365_jobs.start("fixture", "SlowJob")
        return job
    try:
        job = asyncio.run(scenario())
        assert session_tree._closed
        assert server._d365_jobs.status(job["job_id"])["state"] == "stopped"
        assert server._d365_session.state["state"] == "closed"
    finally:
        server._d365_close()


def test_stdio_disconnect_runs_shutdown_before_server_exit(fake_ps, tmp_path, monkeypatch):  # noqa: F811
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    import json
    pidfile, receipt = tmp_path / "child.pid", tmp_path / "closed"
    script = (
        "from pathlib import Path; "
        "from boring_pondnt.d365_server import create_server; "
        f"s=create_server({fake_ps!r},Path({str(tmp_path)!r})); "
        "s.run('stdio'); "
        f"Path({str(receipt)!r}).write_text(str(s._d365_session.state['state']))"
    )
    env = dict(os.environ, FAKE_PS_CHILD_PID_FILE=str(pidfile),
               PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    params = StdioServerParameters(command=sys.executable, args=["-c", script], env=env)
    async def scenario():
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=15) as client:
                await client.initialize()
                deadline = time.monotonic() + 15
                while True:
                    response = await client.call_tool("d365_status", {})
                    status = json.loads(response.content[0].text)
                    if status["session"]["state"] == "ready":
                        break
                    assert time.monotonic() < deadline
                    await asyncio.sleep(0.05)
                response = await client.call_tool("d365_db_sync", {"modules": ["SlowJob"]})
                assert not response.is_error
                pid = await asyncio.to_thread(wait_file, pidfile)
                return int(pid)
    pid = asyncio.run(scenario())
    assert wait_file(receipt) == "closed"
    assert wait_dead(pid, 10)


def test_job_spawn_racing_shutdown_is_not_lost(fake_ps, tmp_path, monkeypatch):  # noqa: F811
    entered, release = threading.Event(), threading.Event()
    actual = d365.Tree
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return actual(*args, **kwargs)
    monkeypatch.setattr(d365, "Tree", slow)
    jobs = d365.Jobs(fake_ps, tmp_path)
    started, failures = [], []
    def start():
        try:
            started.append(jobs.start("fixture", "SlowJob"))
        except BaseException as error:
            failures.append(error)
    worker = threading.Thread(target=start)
    closer = threading.Thread(target=jobs.shutdown)
    worker.start()
    try:
        assert entered.wait(3)
        closer.start()
        release.set()
        worker.join(10)
        closer.join(10)
        assert not worker.is_alive() and not closer.is_alive()
        assert not failures and len(started) == 1
        assert jobs.status(started[0]["job_id"])["state"] == "stopped"
        assert jobs._job(started[0]["job_id"])["tree"]._closed
    finally:
        release.set()
        worker.join(10)
        jobs.shutdown()
        if closer.ident is not None:
            closer.join(10)


def test_competing_syncs_prepare_and_start_only_once(fake_ps, tmp_path):  # noqa: F811
    from concurrent.futures import ThreadPoolExecutor
    from boring_service_kit.outcomes import Refused
    barrier = threading.Barrier(2)
    jobs = d365.Jobs(fake_ps, tmp_path)
    prepare = Mock()
    def attempt():
        barrier.wait(5)
        try:
            return jobs.start("sync", "SlowJob", prepare=prepare)["job_id"]
        except Refused as error:
            return error.code
    try:
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: attempt(), range(2)))
        assert results.count("locked") == 1
        assert len(jobs.list()) == 1
        prepare.assert_called_once()
    finally:
        jobs.shutdown()


@pytest.mark.parametrize("state,done,confirmed", [
    ("stopping", False, True), ("failed", True, False),
])
def test_unsettled_sync_preserves_logs_and_refuses_successor(tmp_path, state, done, confirmed):
    from boring_service_kit.outcomes import Refused
    jobs = d365.Jobs("must not execute", tmp_path)
    event = threading.Event()
    if done:
        event.set()
    jobs._jobs["old"] = dict(job_id="old", kind="sync", state=state,
                             done=event, cleanup_confirmed=confirmed)
    prepare = Mock()
    with pytest.raises(Refused) as error:
        jobs.start("sync", "must not execute", prepare=prepare)
    assert error.value.code == "locked"
    assert jobs.pending("sync") == ["old"]
    prepare.assert_not_called()
