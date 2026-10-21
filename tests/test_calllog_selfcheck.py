"""Call log and self-check."""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from boring_service_kit.calllog import summarize
from boring_pondnt.selfcheck import run_checks
from boring_pondnt.server import create_server


def test_call_log_records_outcomes_without_contents(tmp_path):
    server = create_server(state_dir=tmp_path / "state", log_calls=True)
    target = tmp_path / "a.txt"
    asyncio.run(server.call_tool("file_write", {"path": str(target), "content": "secret" * 100,
                                                "expected_sha256": None}))
    asyncio.run(server.call_tool("file_read", {"path": "relative"}))
    lines = [json.loads(line) for line in (tmp_path / "state" / "calls.jsonl").read_text().splitlines()]
    assert [(e["tool"], e["outcome"], e["code"]) for e in lines] == [
        ("file_write", "succeeded", None), ("file_read", "refused", "invalid_request")]
    assert lines[0]["args"]["content"] == {"chars": 600}
    assert "secret" not in (tmp_path / "state" / "calls.jsonl").read_text()
    assert lines[0]["result_bytes"] > 0 and lines[0]["ms"] >= 0


def test_no_call_log_unless_asked(tmp_path):
    server = create_server(state_dir=tmp_path / "state")
    asyncio.run(server.call_tool("file_list", {"path": str(tmp_path)}))
    assert not (tmp_path / "state" / "calls.jsonl").exists()


def test_summarize_counts_commands_and_operations_without_contents():
    long_command = "x" * 500
    summary = summarize({"command": long_command, "edits": [{}, {}],
                         "operations": [{"method": "file_read"}, {"method": "exec"}], "data": "AAAA"})
    assert summary["command"] == {"chars": 500}
    assert summary["edits"] == {"edits": 2}
    assert summary["operations"] == {"items": 2}
    assert "x" * 200 not in json.dumps(summary)
    assert "file_read" not in json.dumps(summary)
    assert summary["data"] == {"chars": 4}


@pytest.mark.skipif(sys.platform == "win32", reason="the Windows self-check runs on Windows CI and the VM")
def test_self_check_passes_here(tmp_path):
    report = run_checks(tmp_path / "state")
    failed = [c for c in report["checks"] if not c["ok"]]
    assert report["ok"], failed
    assert {c["name"] for c in report["checks"]} >= {"state_dir", "shell_sh", "timeout", "text_round_trip", "job"}


def test_first_error_reads_clixml():
    from boring_pondnt.selfcheck import first_error

    clixml = ('#< CLIXML\r\n<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04">'
              '<S S="Error">Exception calling "Open" with "0" argument(s): "Cannot open database &quot;X&quot;'
              '_x000D__x000A_</S><S S="Error">second line_x000D__x000A_</S></Objs>')
    assert first_error(clixml) == 'Exception calling "Open" with "0" argument(s): "Cannot open database "X"'
    assert first_error("plain error\nmore") == "plain error"
    assert first_error("") == "failed"
