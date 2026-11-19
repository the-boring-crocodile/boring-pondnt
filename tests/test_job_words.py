"""Agent job words match the Crocodile; process settlement keeps its own vocabulary."""
import pytest
from boring_pondnt import jobs


@pytest.mark.parametrize("ending,exit_code,state,timed_out", [
    ("exit", 0, "completed", False),
    ("exit", 2, "completed", False),
    ("timeout", None, "completed", True),
    ("stop", None, "cancelled", False),
])
def test_process_job_reports_crocodile_state(tmp_path, ending, exit_code, state, timed_out):
    class Tree:
        def wait(self, timeout):
            return exit_code
        def close(self):
            pass
    manager = jobs.Jobs(tmp_path)
    directory = manager.root / "fixture"
    directory.mkdir()
    job = jobs.Job("fixture", "synthetic", "sh", str(tmp_path), 0 if ending == "timeout" else 10,
                   directory, 0, tree=Tree())
    if ending == "stop":
        job.stop_requested.set()
    manager._jobs[job.job_id] = job
    try:
        manager._watch(job)
        status = manager.status(job.job_id)
        assert status["state"] == state
        assert status["timed_out"] is timed_out
        assert status["exit_code"] == exit_code
        waited = manager.wait([job.job_id], 0)
        assert waited["settled"] and not waited["wait_expired"]
        assert "all_ended" not in waited
        assert waited["elapsed_seconds"] >= 0
        assert manager.output(job.job_id)["state"] == state
    finally:
        manager.shutdown()


def test_unknown_wait_id_is_a_settled_refusal(tmp_path):
    manager = jobs.Jobs(tmp_path)
    try:
        answer = manager.wait(["0" * 32], 0)
        assert answer["settled"] and not answer["wait_expired"]
        assert answer["jobs"][0]["refusal"]["code"] == "not_found"
    finally:
        manager.shutdown()
