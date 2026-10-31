"""collect.Runs: a background answer belongs to whoever collects it, once."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from boring_pondnt.collect import Runs, StillRunning


class Busy(Exception):
    pass


@pytest.fixture
def runs():
    executor = ThreadPoolExecutor(max_workers=4)
    try:
        yield Runs(executor)
    finally:
        executor.shutdown(wait=True)


def busy(run):
    return Busy(run.label, run.future.done())


def test_answers_within_budget_and_closes(runs):
    run = runs.start("k", "quick", lambda: 42, refuse=busy)
    assert runs.collect("k", run, 5) == 42
    assert runs.get("k") is None


def test_running_and_uncollected_runs_refuse_a_new_start(runs):
    go = threading.Event()
    run = runs.start("k", "slow", go.wait, 5, refuse=busy)
    with pytest.raises(StillRunning) as running:
        runs.collect("k", run, 0.05)
    assert running.value.operation == "slow" and running.value.as_dict("h")["status"] == "running"
    with pytest.raises(Busy) as refused:
        runs.start("k", "next", lambda: None, refuse=busy)
    assert refused.value.args == ("slow", False)
    go.set()
    run.future.result(timeout=5)
    with pytest.raises(Busy) as refused:  # finished, not collected: still refused
        runs.start("k", "next", lambda: None, refuse=busy)
    assert refused.value.args == ("slow", True)
    assert runs.collect("k", run, 5) is True
    assert runs.start("k", "next", lambda: 1, refuse=busy) is not None  # collected: free again
    assert runs.start("other", "x", lambda: 2, refuse=busy) is not None  # keys are independent


def test_an_error_is_the_result_and_is_collected_once(runs):
    def fail():
        raise TimeoutError("the step's own timeout, not the collector's")  # not mistaken for running
    run = runs.start("k", "failing", fail, refuse=busy)
    with pytest.raises(TimeoutError):
        runs.collect("k", run, 5)
    assert runs.get("k") is None


def test_admit_runs_under_the_lock_and_refuses_before_submitting(runs):
    ran = []
    with pytest.raises(Busy):
        runs.start("k", "x", lambda: ran.append(1), refuse=busy, admit=lambda: (_ for _ in ()).throw(Busy()))
    assert ran == [] and runs.get("k") is None


def test_collectors_of_one_run_share_it_and_never_close_a_newer_one(runs):
    go = threading.Event()
    first = runs.start("k", "a", go.wait, 5, refuse=busy, owner="W1")
    answers = []
    collectors = [threading.Thread(target=lambda: answers.append(runs.collect("k", first, 5))) for _ in range(2)]
    for thread in collectors:
        thread.start()
    go.set()
    for thread in collectors:
        thread.join(5)
    assert answers == [True, True] and runs.get("k") is None
    second = runs.start("k", "b", lambda: "B", refuse=busy)
    assert runs.collect("k", first, 5) is True  # a late collector of the first run
    assert runs.get("k") is second
    assert first.owner == "W1"
