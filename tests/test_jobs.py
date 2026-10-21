"""Jobs: start, wait, read, stop; trees die on timeout, stop and shutdown."""

from __future__ import annotations

import sys

import pytest

from boring_pondnt.jobs import Jobs
from boring_service_kit.outcomes import Refused
from test_execs import wait_dead

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX commands; Windows covered by test_execs")


@pytest.fixture
def jobs(tmp_path):
    manager = Jobs(tmp_path / "state")
    yield manager
    manager.shutdown()


def test_start_wait_status_output(jobs):
    started = jobs.start("i=1; while [ $i -le 300 ]; do echo line$i; i=$((i+1)); done; echo oops >&2; exit 2")
    assert started["state"] == "running" and len(started["job_id"]) == 32
    waited = jobs.wait([started["job_id"]], 10)
    assert waited["all_ended"]
    status = waited["jobs"][0]
    assert status["state"] == "exited" and status["exit_code"] == 2
    tail = jobs.output(started["job_id"], tail_lines=3)
    assert tail["content"] == "line298\nline299\nline300\n"
    assert tail["start_line"] == 298 and tail["total_lines"] == 300
    head = jobs.output(started["job_id"], start_line=1, line_count=2)
    assert head["content"] == "line1\nline2\n"
    assert jobs.output(started["job_id"], stream="stderr")["content"] == "oops\n"


def test_wait_returns_after_wait_seconds_when_still_running(jobs):
    job = jobs.start("sleep 30")
    waited = jobs.wait([job["job_id"]], 0.3)
    assert not waited["all_ended"] and waited["jobs"][0]["state"] == "running"


def test_timeout_kills_the_tree(jobs, tmp_path):
    pidfile = tmp_path / "child.pid"
    job = jobs.start(f"sleep 30 & echo $! > {pidfile}; sleep 30", timeout_seconds=0.5)
    status = jobs.wait([job["job_id"]], 10)["jobs"][0]
    assert status["state"] == "timed_out" and status["exit_code"] is None
    assert wait_dead(int(pidfile.read_text()))


def test_stop_kills_the_tree(jobs, tmp_path):
    pidfile = tmp_path / "child.pid"
    job = jobs.start(f"sleep 30 & echo $! > {pidfile}; sleep 30")
    jobs.wait([job["job_id"]], 0.3)
    stopped = jobs.stop(job["job_id"])
    assert stopped["state"] == "stopped"
    assert wait_dead(int(pidfile.read_text()))
    assert jobs.stop(job["job_id"])["state"] == "stopped"  # stopping again is harmless


def test_shutdown_kills_running_jobs(tmp_path):
    manager = Jobs(tmp_path / "state")
    pidfile = tmp_path / "child.pid"
    job = manager.start(f"echo $$ > {pidfile}; sleep 30")
    manager.wait([job["job_id"]], 0.3)
    manager.shutdown()
    assert manager.status(job["job_id"])["state"] == "stopped"
    assert wait_dead(int(pidfile.read_text()))


def test_list_newest_first(jobs):
    first = jobs.start("true")
    second = jobs.start("true")
    jobs.wait([first["job_id"], second["job_id"]], 10)
    listed = jobs.list()
    assert [j["job_id"] for j in listed["jobs"]][:2] == [second["job_id"], first["job_id"]]


def test_refusals(jobs):
    with pytest.raises(Refused) as info:
        jobs.status("0" * 32)
    assert info.value.code == "not_found"
    job = jobs.start("true")
    for call in (
        lambda: jobs.wait([], 1),
        lambda: jobs.wait([job["job_id"]], 61),
        lambda: jobs.output(job["job_id"], stream="both"),
        lambda: jobs.output(job["job_id"], tail_lines=5, start_line=1),
        lambda: jobs.output(job["job_id"], tail_lines=0),
        lambda: jobs.start("true", timeout_seconds=86_401),
    ):
        with pytest.raises(Refused) as info:
            call()
        assert info.value.code == "invalid_request"
