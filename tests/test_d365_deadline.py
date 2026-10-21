"""Session.call: one deadline from admission to answer.

No PowerShell runs: the session's process is a stub whose stdin records what
would have been sent, so "never sent" is observable.
"""

from __future__ import annotations

import io
import threading
import time
from types import SimpleNamespace

import pytest

from boring_pondnt import d365
from boring_service_kit.outcomes import Failed, Refused

RESULT = '@@RESULT {"ok": true, "value": "ran"}'


def stub_session(call_seconds: float, answer=lambda timeout: RESULT) -> d365.Session:
    session = d365.Session("never-executed", call_seconds=call_seconds)
    session.state = {"state": "ready"}
    session._process = SimpleNamespace(stdin=io.BytesIO(), poll=lambda: None)
    session._next_line = answer
    session.restarts = []
    session._stop_process = lambda: session.restarts.append("stop")
    session.start = lambda: session.restarts.append("start")
    return session


def hold_lock(session: d365.Session, seconds: float) -> threading.Thread:
    """Another call occupying the session for `seconds`; returns once it holds the lock."""
    held = threading.Event()

    def busy() -> None:
        with session._lock:
            held.set()
            time.sleep(seconds)

    thread = threading.Thread(target=busy)
    thread.start()
    assert held.wait(5)
    return thread


def test_a_call_queued_beyond_its_budget_is_refused_and_never_sent():
    session = stub_session(call_seconds=0.1)
    other = hold_lock(session, 5)
    started = time.monotonic()
    with pytest.raises(Refused) as refused:
        session.call("Stop-D365Environment -All")
    elapsed = time.monotonic() - started
    other.join()
    assert refused.value.code == "busy" and "not sent" in refused.value.message
    assert elapsed < 4  # the budget (0.1 s) bounded the wait, not the other call (5 s)
    assert session._process.stdin.getvalue() == b""


def test_a_call_queued_within_its_budget_runs():
    session = stub_session(call_seconds=2)
    other = hold_lock(session, 0.1)
    assert session.call("Get-D365Model") == "ran"
    other.join()
    assert session._process.stdin.getvalue()


def test_a_dispatched_call_that_times_out_is_uncertain_not_refused():
    def silent(timeout: float) -> None:
        time.sleep(min(timeout, 0.2))
        return None

    session = stub_session(call_seconds=0.1, answer=silent)
    with pytest.raises(Failed) as failed:
        session.call("Stop-D365Environment -All")
    assert failed.value.code == "timeout" and failed.value.details["dispatched"] is True
    assert "effect is unknown" in failed.value.message
    assert session._process.stdin.getvalue()  # it was sent
    assert session.restarts == ["stop", "start"]


def test_queueing_counts_against_the_answer_time():
    """Time spent waiting for the session leaves less for the answer: one deadline."""
    waited: list[float] = []

    def slow(timeout: float) -> None:
        waited.append(timeout)
        time.sleep(timeout)
        return None

    session = stub_session(call_seconds=3, answer=slow)
    other = hold_lock(session, 1.5)
    with pytest.raises(Failed):
        session.call("Get-D365Model")
    other.join()
    # At least 1.5 s were spent queued, so at most 1.5 s were left to answer
    # (a loaded host only queues longer, leaving less): one deadline, not two.
    assert waited and waited[0] <= 1.6  # 3 s budget: well below it; Windows timers are coarse


# d365_sql: a script of GO batches is one call with one deadline.

class RecordingConnection:
    """Stands in for the ODBC connection: records every batch sent; the first takes `seconds`."""

    autocommit = True
    description = None
    rowcount = 1

    def __init__(self, seconds: float = 0) -> None:
        self.seconds = seconds
        self.sent: list[str] = []
        self.rolled_back = False

    def cursor(self) -> "RecordingConnection":
        return self

    def execute(self, batch: str) -> None:
        self.sent.append(batch)
        if len(self.sent) == 1:
            time.sleep(self.seconds)

    def nextset(self) -> bool:
        return False

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        pass


SCRIPT = "DELETE FROM A\nGO\nDELETE FROM B\nGO\nDELETE FROM C"


def sql_call(tmp_path, connection: RecordingConnection, **arguments):
    from boring_pondnt import d365_server
    from test_d365 import call

    server = d365_server.create_server(state_dir=tmp_path, start_session=False,
                                       sql_connect=lambda database, timeout: lambda: connection)
    try:
        return call(server, "d365_sql", {"sql": SCRIPT, **arguments})
    finally:
        server._d365_close()


@pytest.mark.parametrize("atomic", [False, True])
def test_no_sql_batch_starts_after_the_scripts_timeout(tmp_path, atomic):
    connection = RecordingConnection(seconds=1.5)  # the first batch outlasts the 1 s timeout
    is_error, data = sql_call(tmp_path, connection, timeout=1, atomic=atomic)
    assert connection.sent == ["DELETE FROM A"]  # batches 2 and 3 were never sent
    assert is_error and data["code"] == "timeout" and data["status"] == "failed"
    assert data["details"]["batches_done"] == 1 and data["details"]["batch"] == 2
    assert data["details"]["rolled_back"] is atomic and connection.rolled_back is atomic


def test_a_sql_script_within_its_timeout_runs_every_batch(tmp_path):
    connection = RecordingConnection()
    is_error, data = sql_call(tmp_path, connection, timeout=5)
    assert not is_error and data["batches"] == 3 and len(connection.sent) == 3


def test_a_sql_script_whose_deadline_passed_before_its_first_batch_is_refused():
    from boring_pondnt import sqlserver

    connection = RecordingConnection()
    with pytest.raises(Refused) as refused:
        sqlserver.execute(lambda: connection, SCRIPT, deadline=time.monotonic() - 1)
    assert refused.value.code == "not_admitted" and connection.sent == []
