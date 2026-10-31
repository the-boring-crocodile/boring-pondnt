"""The MCP layer: compact results, outcomes as data, a real stdio round trip."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from boring_pondnt.server import create_server

EXPECTED_TOOLS = {
    "bootstrap", "file_read", "file_write", "file_patch", "file_list",
    "file_search", "file_read_bytes", "file_write_bytes", "exec",
    "job_start", "job_wait", "job_output", "job_stop", "job_list", "batch",
}


def call(server, name, arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    assert len(result.content) == 1
    text = result.content[0].text
    assert "\n  " not in text  # compact, not indented
    return result.is_error, json.loads(text)


def test_tools_are_few_strict_and_schema_free():
    tools = asyncio.run(create_server().list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.input_schema.get("additionalProperties") is False, tool.name
        assert tool.output_schema is None, tool.name  # no schema re-sent with every request
        assert len(tool.description) <= 120, tool.name
    listed = json.dumps([t.model_dump(exclude_none=True, by_alias=True) for t in tools])
    assert len(listed) < 8000  # the whole tools/list, re-sent every request
    assert '"title"' not in listed and '"anyOf"' not in listed


def test_lean_schema_still_validates_optional_and_required(tmp_path):
    server = create_server()
    is_error, _ = call(server, "file_list", {"path": str(tmp_path), "pattern": None})
    assert not is_error  # null still accepted where the type allows it
    with pytest.raises(ToolError):
        asyncio.run(server.call_tool("file_list", {}))  # required still required


def test_results_are_one_compact_json_text(tmp_path):
    path = tmp_path / "a.xml"
    path.write_bytes(b"\xef\xbb\xbf<a/>\r\n")
    server = create_server()
    is_error, data = call(server, "file_read", {"path": str(path)})
    assert not is_error
    assert data["content"] == "<a/>\n" and data["bom"] and data["newline"] == "crlf"
    is_error, data = call(server, "file_patch", {
        "path": str(path), "expected_sha256": data["sha256"],
        "edits": [{"old_text": "<a/>", "new_text": "<b/>"}],
    })
    assert not is_error and path.read_bytes() == b"\xef\xbb\xbf<b/>\r\n"


def test_refusals_are_errors_carrying_the_outcome(tmp_path):
    server = create_server()
    is_error, data = call(server, "file_read", {"path": "relative.txt"})
    assert is_error
    assert data == {"status": "refused", "code": "invalid_request", "message": "path must be absolute",
                    "details": {"path": "relative.txt"}}
    (tmp_path / "x").write_text("x")
    is_error, data = call(server, "file_write", {"path": str(tmp_path / "x"), "content": "y",
                                                 "expected_sha256": None})
    assert is_error and data["code"] == "stale" and "current_sha256" in data["details"]


def test_unknown_arguments_are_refused(tmp_path):
    with pytest.raises(ToolError, match="Extra inputs are not permitted|unknown: recursive"):
        asyncio.run(create_server().call_tool("file_list", {"path": str(tmp_path), "recursive": True}))


def test_argument_errors_are_one_short_line():
    with pytest.raises(ToolError) as raised:
        asyncio.run(create_server().call_tool("job_wait", {"job_id": "x", "timeout_seconds": 4}))
    message = str(raised.value)
    assert "unknown: job_id, timeout_seconds" in message and "missing: job_ids" in message
    assert "Parameters: job_ids, wait_seconds" in message
    assert "errors.pydantic.dev" not in message and "\n" not in message
    with pytest.raises(ToolError, match=r"invalid: wait_seconds \("):
        asyncio.run(create_server().call_tool("job_wait", {"job_ids": ["x"], "wait_seconds": "soon"}))


def test_bootstrap_with_and_without_kit(tmp_path):
    is_error, data = call(create_server(), "bootstrap", {})
    assert not is_error and data["handbook"] is None and data["version"]
    kit = tmp_path / "kit"
    kit.mkdir()
    (kit / "bootstrap.md").write_bytes(b"\xef\xbb\xbf# Start\r\nRead agreement.md.\r\n")
    (kit / "agreement.md").write_text("# Agreement\n")
    is_error, data = call(create_server(kit), "bootstrap", {})
    assert not is_error
    assert data["bootstrap"].startswith("# Start")
    assert [p.endswith("agreement.md") for p in data["handbook_files"]] == [True]


def test_no_stdio_any_more():
    """The core speaks HTTP only; started without --config it refuses and says why."""
    result = subprocess.run([sys.executable, "-m", "boring_pondnt.server"], capture_output=True, text=True,
                            timeout=60, env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    assert result.returncode == 2 and "--config is required" in result.stderr


def batch(server, operations, **kwargs):
    return call(server, "batch", {"operations": operations, **kwargs})


def test_batch_runs_steps_in_order(tmp_path):
    path = tmp_path / "a.txt"
    path.write_bytes(b"one\r\ntwo\r\n")
    server = create_server(state_dir=tmp_path / "state")
    is_error, data = call(server, "file_read", {"path": str(path)})
    is_error, data = batch(server, [
        {"id": "patch", "method": "file_patch", "arguments": {
            "path": str(path), "expected_sha256": data["sha256"],
            "edits": [{"old_text": "two", "new_text": "zwei"}]}},
        {"id": "read", "method": "file_read", "arguments": {"path": str(path)}},
        {"id": "list", "method": "file_list", "arguments": {"path": str(tmp_path)}},
    ])
    assert not is_error and data["all_attempted"] and data["stop_reason"] is None
    assert [r["status"] for r in data["results"]] == ["succeeded"] * 3
    assert data["results"][1]["result"]["content"] == "one\nzwei\n"
    assert path.read_bytes() == b"one\r\nzwei\r\n"


def test_batch_preflight_refuses_before_any_effect(tmp_path):
    server = create_server(state_dir=tmp_path / "state")
    target = tmp_path / "new.txt"
    for bad in (
        {"id": "b", "method": "rm_rf", "arguments": {}},
        {"id": "b", "method": "file_read", "arguments": {"path": "x", "bogus": 1}},
        {"id": "b", "method": "file_read", "arguments": {}},
        {"id": "a", "method": "file_read", "arguments": {"path": "x"}},  # duplicate id
        {"id": "b", "method": "bootstrap", "arguments": {}},
    ):
        is_error, data = batch(server, [
            {"id": "a", "method": "file_write", "arguments": {
                "path": str(target), "content": "x", "expected_sha256": None}},
            bad,
        ])
        assert is_error and data["code"] == "invalid_request", bad
        assert not target.exists(), bad


def test_batch_stop_and_continue(tmp_path):
    server = create_server(state_dir=tmp_path / "state")
    steps = [
        {"id": "missing", "method": "file_read", "arguments": {"path": str(tmp_path / "nope")}},
        {"id": "list", "method": "file_list", "arguments": {"path": str(tmp_path)}},
    ]
    _, stopped = batch(server, steps)
    assert [r["status"] for r in stopped["results"]] == ["refused", "not_started"]
    assert stopped["results"][0]["refusal"]["code"] == "not_found"
    assert stopped["stop_reason"] == "step_refused" and not stopped["all_attempted"]
    _, continued = batch(server, steps, on_error="continue")
    assert [r["status"] for r in continued["results"]] == ["refused", "succeeded"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX command")
def test_batch_starts_and_waits_for_a_job(tmp_path):
    server = create_server(state_dir=tmp_path / "state")
    _, started = batch(server, [{"id": "s", "method": "job_start", "arguments": {"command": "echo built"}}])
    job_id = started["results"][0]["result"]["job_id"]
    _, data = batch(server, [
        {"id": "w", "method": "job_wait", "arguments": {"job_ids": [job_id], "wait_seconds": 10}},
        {"id": "o", "method": "job_output", "arguments": {"job_id": job_id}},
    ])
    assert data["results"][0]["result"]["all_ended"]
    assert data["results"][1]["result"]["content"] == "built\n"


def test_batch_stops_at_the_result_limit(tmp_path, monkeypatch):
    from boring_pondnt import server as server_module

    monkeypatch.setattr(server_module, "BATCH_RESULT_MAX", 1000)  # one result is ~750
    (tmp_path / "big.txt").write_text("x" * 400 + "\n")
    server = create_server(state_dir=tmp_path / "state")
    steps = [{"id": f"r{i}", "method": "file_read", "arguments": {"path": str(tmp_path / "big.txt")}}
             for i in range(4)]
    _, data = batch(server, steps)
    assert [r["status"] for r in data["results"]] == ["succeeded", "succeeded", "not_started", "not_started"]
    assert data["stop_reason"] == "result_limit" and data["result_chars"] > 1000


def test_lean_schema_keeps_parameters_named_like_annotations():
    from boring_service_kit.tools import lean_schema

    schema = {"title": "argsModel", "type": "object", "required": ["title"],
              "properties": {"title": {"title": "Title", "type": "string"},
                             "default": {"title": "Default", "anyOf": [{"type": "string"}, {"type": "null"}],
                                         "default": None}}}
    lean = lean_schema(schema)
    assert "title" not in lean and set(lean["properties"]) == {"title", "default"}
    assert lean["properties"]["title"] == {"type": "string"}
    assert lean["properties"]["default"] == {"type": ["string", "null"]}


def test_every_required_parameter_is_published():
    for tool in asyncio.run(create_server().list_tools()):
        assert set(tool.input_schema.get("required", [])) <= set(tool.input_schema["properties"]), tool.name


@pytest.mark.parametrize("method", ["xref-names", "xref_names", "xref", "d365_build", "d365_run"])
def test_batch_names_the_d365_server_for_its_tools(tmp_path, method):
    # Copilot put "xref-names" into a core batch on a dev VM.
    server = create_server(state_dir=tmp_path / "state")
    is_error, data = batch(server, [{"id": "a", "method": method, "arguments": {}}])
    assert is_error and data["code"] == "invalid_request"
    assert "d365 server" in json.dumps(data) and "call it directly" in json.dumps(data)


def test_tool_schemas_are_strict_for_copilot():
    from strict_schema import assert_copilot_accepts
    assert_copilot_accepts(asyncio.run(create_server().list_tools()))
