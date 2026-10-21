"""Exercise the public batch boundary without real shells or sleeping."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from boring_pondnt import server, execs, jobs


def call(app, steps, **options):
    answer = asyncio.run(app.call_tool("batch", {"operations": steps, **options}))
    return answer.is_error, json.loads(answer.content[0].text)


@pytest.mark.parametrize("result", [
    {"exit_code": 7, "timed_out": False, "stdout": "diagnostic"},
    {"exit_code": None, "timed_out": True, "stdout": "partial"},
])
@pytest.mark.parametrize("policy", ["stop", "continue"])
def test_command_outcome_controls_later_effects(tmp_path, monkeypatch, result, policy):
    monkeypatch.setattr(execs, "run", lambda *args: result)
    target = tmp_path / "after"
    app = server.create_server(state_dir=tmp_path / "state")
    error, data = call(app, [
        {"id": "command", "method": "exec", "arguments": {"command": "synthetic"}},
        {"id": "write", "method": "file_write", "arguments":
            {"path": str(target), "content": "later", "expected_sha256": None}},
    ], on_error=policy)
    assert not error
    assert data["results"][0]["status"] == "failed"
    assert data["results"][0]["result"] == result
    assert target.exists() == (policy == "continue")
    assert data["stop_reason"] == ("step_failed" if policy == "stop" else None)


def test_remaining_budget_reaches_each_waiting_owner(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=lambda: now[0]))
    budgets = []
    def command(*args):
        budgets.append(("exec", args[-1]))
        now[0] += 0.6
        return {"exit_code": 0, "timed_out": False}
    def wait(self, ids, seconds):
        budgets.append(("wait", seconds))
        now[0] += seconds
        return {"all_ended": False}
    monkeypatch.setattr(execs, "run", command)
    monkeypatch.setattr(jobs.Jobs, "wait", wait)
    app = server.create_server(state_dir=tmp_path / "state")
    error, data = call(app, [
        {"id": "exec", "method": "exec", "arguments": {"command": "synthetic", "timeout_seconds": 60}},
        {"id": "wait", "method": "job_wait", "arguments": {"job_ids": ["synthetic"], "wait_seconds": 30}},
        {"id": "later", "method": "job_list"},
    ], timeout_seconds=1)
    assert not error
    assert budgets[0] == ("exec", 1)
    assert budgets[1][0] == "wait"
    assert budgets[1][1] == pytest.approx(0.4)
    assert data["results"][-1]["status"] == "not_started"
    assert data["stop_reason"] == "timeout"


@pytest.mark.parametrize("method", [[], {}])
def test_non_string_method_is_a_preflight_refusal(tmp_path, method):
    app = server.create_server(state_dir=tmp_path / "state")
    target = tmp_path / "before"
    error, data = call(app, [
        {"id": "write", "method": "file_write", "arguments":
            {"path": str(target), "content": "must not happen", "expected_sha256": None}},
        {"id": "bad", "method": method},
    ])
    assert error and data["code"] == "invalid_request"
    assert not target.exists()


@pytest.mark.parametrize("method,arguments", [
    ("exec", {"command": " "}),
    ("exec", {"command": "synthetic", "timeout_seconds": 0}),
    ("exec", {"command": "synthetic", "timeout_seconds": 601}),
    ("exec", {"command": "synthetic", "shell": "unrecognized"}),
    ("exec", {"command": "synthetic", "cwd": "relative"}),
    ("job_start", {"command": " "}),
    ("job_start", {"command": "synthetic", "timeout_seconds": 86401}),
    ("job_wait", {"job_ids": []}),
    ("job_wait", {"job_ids": ["unknown"], "wait_seconds": -1}),
    ("job_wait", {"job_ids": ["unknown"], "wait_seconds": 61}),
    ("job_output", {"job_id": "unknown", "stream": "other"}),
    ("job_output", {"job_id": "unknown", "tail_lines": 1, "start_line": 1}),
    ("job_output", {"job_id": "unknown", "line_count": 0}),
    ("job_output", {"job_id": "unknown", "start_line": 0}),
    ("job_list", {"limit": 0}),
    ("file_read", {"start_line": 0}),
    ("file_read", {"line_count": 0}),
    ("file_list", {"limit": 0}),
    ("file_search", {"query": ""}),
    ("file_search", {"query": "two\nlines"}),
    ("file_search", {"query": "x", "limit": 0}),
    ("file_read_bytes", {"offset": -1}),
    ("file_read_bytes", {"length": 0}),
    ("file_write_bytes", {"data": "***", "expected_sha256": None}),
    ("file_write_bytes", {"data": "", "expected_sha256": None, "offset": 1}),
    ("file_write", {"content": "", "expected_sha256": None, "encoding": "latin-1"}),
    ("file_write", {"content": "", "expected_sha256": None, "newline": "cr"}),
    ("file_patch", {"expected_sha256": "old", "edits": [], "newline": "cr"}),
    ("file_patch", {"expected_sha256": "old", "edits": []}),
    ("file_patch", {"expected_sha256": "old", "edits": [{"old_text": "", "new_text": "new"}]}),
    ("file_patch", {"expected_sha256": "old", "edits": [
        {"old_text": "first", "new_text": "ok"}, {"old_text": "", "new_text": "bad"}]}),
    ("file_patch", {"expected_sha256": "old", "edits": [{"old_text": "x", "new_text": "\x00"}]}),
    ("file_write", {"content": "\x00", "expected_sha256": None}),
])
def test_stateless_options_refuse_before_any_step(tmp_path, method, arguments):
    target = tmp_path / "must-not-be-created"
    state = tmp_path / "state"
    app = server.create_server(state_dir=state)
    if method.startswith("file_"):
        arguments = {"path": str(tmp_path / "not-yet-existing"), **arguments}
    error, data = call(app, [
        {"id": "effect", "method": "file_write", "arguments":
            {"path": str(target), "content": "forbidden", "expected_sha256": None}},
        {"id": "bad", "method": method, "arguments": arguments},
    ], on_error="continue")
    assert error and data["code"] == "invalid_request"
    assert data["details"]["step"] == "bad"
    assert not target.exists() and not state.exists()


def test_http_call_budget_is_checked_before_earlier_effects(tmp_path):
    target = tmp_path / "must-not-be-created"
    app = server.create_server(state_dir=tmp_path / "state", call_budget=2)
    error, data = call(app, [
        {"id": "effect", "method": "file_write", "arguments":
            {"path": str(target), "content": "forbidden", "expected_sha256": None}},
        {"id": "bad", "method": "exec", "arguments": {"command": "synthetic", "timeout_seconds": 3}},
    ])
    assert error and data["code"] == "invalid_request" and not target.exists()


def test_preflight_allows_files_created_by_earlier_steps(tmp_path):
    target = tmp_path / "created"
    app = server.create_server(state_dir=tmp_path / "state")
    error, data = call(app, [
        {"id": "write", "method": "file_write", "arguments":
            {"path": str(target), "content": "hello", "expected_sha256": None}},
        {"id": "read", "method": "file_read", "arguments": {"path": str(target)}},
    ])
    assert not error and all(step["status"] == "succeeded" for step in data["results"])
    assert data["results"][1]["result"]["content"] == "hello\n"
