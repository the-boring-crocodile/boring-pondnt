"""boring-pondnt-d365 against a stand-in PowerShell (tests/fake_powershell.py).

The real thing runs on the dev box; here: the session protocol, the tools'
shapes and bounds, jobs, refusals -- and that no password leaves the server.
"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
import time
from pathlib import Path

import pytest

from boring_pondnt.d365 import Session
from boring_pondnt.d365_server import create_server, redact

FAKE = Path(__file__).with_name("fake_powershell.py")
SECRET = "p@ss=w;rd"


@pytest.fixture(scope="module")
def fake_ps(tmp_path_factory):
    folder = tmp_path_factory.mktemp("ps")
    if sys.platform == "win32":  # a .cmd stands in for powershell.exe
        launcher = folder / "powershell.cmd"
        launcher.write_text(f'@"{sys.executable}" "{FAKE}" %*\r\n')
        return str(launcher)
    launcher = folder / "powershell"
    launcher.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return str(launcher)


def call(server, name, args=None):
    result = asyncio.run(server.call_tool(name, args or {}))
    blocks = result[0] if isinstance(result, tuple) else result
    content = blocks.content if hasattr(blocks, "content") else blocks
    text = content[0].text
    assert SECRET not in text, f"secret leaked by {name}"
    return getattr(blocks, "is_error", False), json.loads(text)


def ready_server(fake_ps, tmp_path, **kw):
    server = create_server(fake_ps, tmp_path, **kw)
    deadline = time.monotonic() + 15
    while server._d365_session.state.get("state") == "loading" and time.monotonic() < deadline:
        time.sleep(0.05)
    return server


@pytest.fixture
def server(fake_ps, tmp_path):
    s = ready_server(fake_ps, tmp_path)
    yield s
    s._d365_close()


def test_redact_masks_passwords_in_every_form():
    text = ("server=x;Password='a=b;c';User Id=u; pwd=secret1 -SqlPwd hunter2 Password=\"q w\" "
            "https://acct.blob/x?sv=1&sig=abcDEF%2B&se=2 AccountKey=Zm9v client_secret: s3cr3t")
    masked = redact({"a": [text]})["a"][0]
    for secret in ("a=b;c", "secret1", "hunter2", "q w", "abcDEF%2B", "Zm9v", "s3cr3t"):
        assert secret not in masked
    assert "User Id=u" in masked and "server=x" in masked
    assert "abc" not in redact("connect=x;Password='abc") and "def" not in redact('Pwd="def')  # cut off, unclosed


def test_tools_are_thirteen_and_small(server):
    tools = asyncio.run(server.list_tools())
    assert sorted(t.name for t in tools) == sorted([
        "d365_status", "d365_models", "d365_build", "d365_results", "d365_db_sync",
        "d365_best_practice", "d365_environment", "d365_job", "d365_sql",
        "xref_info", "xref_names", "xref_references", "xref_hierarchy"])
    assert all(len(t.description or "") <= 120 for t in tools)
    assert len(json.dumps([t.model_dump(exclude_none=True) for t in tools])) < 8500


def test_status_reports_session_and_environment(server):
    is_error, data = call(server, "d365_status")
    assert not is_error
    assert data["session"]["state"] == "ready" and data["session"]["d365fo_tools"] == "0.7.34-fake"
    assert data["elevated"] is True and data["services"][0]["status"] == "Running"


def test_status_right_after_start_answers_at_once(fake_ps, tmp_path):
    """The first d365_status after a (re)start never waits for the session: the environment
    when it is ready, otherwise the state and a note to ask again (a client may give up on
    a slow call and show nothing)."""
    import time
    server = create_server(fake_ps, tmp_path)  # no waiting: the session may still be loading
    try:
        started = time.monotonic()
        is_error, data = call(server, "d365_status")
        assert time.monotonic() - started < 5
        assert not is_error
        if data["session"]["state"] == "ready":
            assert "services" in data
        else:
            assert data["session"]["state"] == "loading" and "still loading" in data["note"]
    finally:
        server._d365_close()


def test_status_while_still_loading_says_so_at_once(server, monkeypatch):
    import time
    session = server._d365_session
    monkeypatch.setattr(session, "state", {"state": "loading", "since": "now"})
    started = time.monotonic()
    is_error, data = call(server, "d365_status")
    assert time.monotonic() - started < 1
    assert not is_error and data["session"]["state"] == "loading"
    assert "still loading" in data["note"] and "services" not in data


def test_models_live(server):
    _, data = call(server, "d365_models", {"exclude_microsoft": True})
    assert data["total"] == 2 and data["models"][1]["name"] == "ContosoExt"
    is_error, data = call(server, "d365_models", {"name": "x; Remove-Item C:\\"})
    assert is_error and data["code"] == "invalid_request"


def test_dry_runs_show_the_command_masked(server):
    for tool, args in (("d365_build", {"module": "ContosoExt", "dry_run": True}),
                       ("d365_db_sync", {"modules": ["ContosoExt"], "dry_run": True}),
                       ("d365_db_sync", {"dry_run": True}),
                       ("d365_best_practice", {"module": "ContosoExt", "model": "ContosoExt", "dry_run": True})):
        is_error, data = call(server, tool, args)
        assert not is_error and data["dry_run"] is True
        assert "SyncEngine.exe" in data["command"] and "Password=***" in data["command"]


def test_names_are_checked_before_powershell_sees_them(server):
    for tool, args in (("d365_build", {"module": "A'; Stop-Computer; '"}),
                       ("d365_db_sync", {"modules": ["ok", "bad name"]}),
                       ("d365_best_practice", {"module": "ok", "model": "$(evil)"})):
        is_error, data = call(server, tool, args)
        assert is_error and data["code"] == "invalid_request"


def test_build_job_runs_and_its_log_is_masked(server):
    _, started = call(server, "d365_build", {"module": "ContosoExt"})
    assert started["state"] in ("running", "exited") and started["kind"] == "build"
    is_error, data = call(server, "d365_job", {"job_id": started["job_id"], "wait_seconds": 15})
    assert not is_error and not data["wait_expired"]
    assert data["exit_code"] == 0
    assert data["tail"][-1] == "Build completed: 0 errors"
    assert any("Password=***" in line for line in data["tail"])
    _, listing = call(server, "d365_job")
    assert listing["jobs"][0]["job_id"] == started["job_id"]


@pytest.mark.parametrize("args", [
    {"stop": True},
    {"wait_seconds": 1},
    {"job_id": "unknown", "stop": True, "wait_seconds": 1},
    {"job_id": "unknown", "wait_seconds": -1},
    {"job_id": "unknown", "wait_seconds": 61},
])
def test_job_rejects_invalid_wait_requests(server, args):
    is_error, data = call(server, "d365_job", args)
    assert is_error and data["code"] == "invalid_request"


def test_job_wait_does_not_restart_or_stop_work(server):
    _, started = call(server, "d365_db_sync", {"modules": ["SlowJob"]})
    try:
        is_error, waiting = call(server, "d365_job", {"job_id": started["job_id"], "wait_seconds": 0})
        assert not is_error and waiting["state"] == "running" and waiting["wait_expired"]
        is_error, invalid = call(server, "d365_job", {
            "job_id": started["job_id"], "stop": True, "tail_lines": 0})
        assert is_error and invalid["code"] == "invalid_request"
        _, still_running = call(server, "d365_job", {"job_id": started["job_id"]})
        assert still_running["state"] == "running"
        _, listing = call(server, "d365_job")
        assert [job["job_id"] for job in listing["jobs"]] == [started["job_id"]]
        is_error, missing = call(server, "d365_job", {"job_id": "unknown", "wait_seconds": 0})
        assert is_error and missing["code"] == "not_found"
    finally:
        call(server, "d365_job", {"job_id": started["job_id"], "stop": True})


@pytest.mark.parametrize("seconds", [-1, 61, float("nan"), float("inf")])
def test_job_wait_owner_refuses_out_of_bounds_before_process_access(tmp_path, seconds):
    from boring_pondnt.d365 import Jobs
    from boring_service_kit.outcomes import Refused
    with pytest.raises(Refused) as error:
        Jobs("unused", tmp_path).wait("unknown", seconds)
    assert error.value.code == "invalid_request"


def test_a_job_can_be_stopped(server):
    _, started = call(server, "d365_db_sync", {"modules": ["SlowJob"]})
    _, stopped = call(server, "d365_job", {"job_id": started["job_id"], "stop": True})
    assert stopped["state"] == "stopped"


DIAGNOSTICS = """<?xml version="1.0" encoding="utf-8"?>
<Diagnostics><Items>{items}</Items><Aliases /></Diagnostics>"""
ITEM = ("<Diagnostic><DiagnosticType>Compile</DiagnosticType><Severity>{sev}</Severity>"
        "<Path>dynamics://Class/C{n}/Method/m</Path><ElementType>Class</ElementType><Moniker>m</Moniker>"
        "<Line>{n}</Line><Column>5</Column><Message>{msg}</Message></Diagnostic>")


def test_compiler_results_from_the_diagnostics_file(fake_ps, tmp_path):
    folder = tmp_path / "d365-logs" / "ContosoExt" / "ContosoExt"
    folder.mkdir(parents=True)
    items = ([ITEM.format(sev="Error", n=n, msg=f"broken {n}") for n in range(1, 8)]
             + [ITEM.format(sev="Warning", n=n, msg="careful") for n in range(20, 23)]
             + [ITEM.format(sev="Informational", n=30, msg="fyi")])
    (folder / "Dynamics.AX.ContosoExt.xppc.xml").write_text(DIAGNOSTICS.format(items="".join(items)))
    s = ready_server(fake_ps, tmp_path)
    try:
        _, data = call(s, "d365_results", {"module": "ContosoExt", "limit": 5})
        assert data["totals"] == {"Error": 7, "Warning": 3, "Informational": 1}
        assert len(data["rows"]) == 5 and data["truncated"] is True
        assert data["kind"] == "compile"
        assert data["rows"][0] == {"severity": "Error", "type": "Compile", "element_type": "Class",
                                   "path": "dynamics://Class/C1/Method/m", "line": 1, "column": 5,
                                   "message": "broken 1"}
        _, data = call(s, "d365_results", {"module": "ContosoExt", "warnings": True, "limit": 500})
        assert [r["severity"] for r in data["rows"]] == ["Error"] * 7 + ["Warning"] * 3
        is_error, data = call(s, "d365_results", {"module": "NoLog"})
        assert is_error and data["code"] == "not_found" and "Visual Studio" in data["message"]
        is_error, data = call(s, "d365_results", {"module": "ContosoExt", "kind": "lint"})
        assert is_error and data["code"] == "invalid_request"
    finally:
        s._d365_close()


@pytest.mark.parametrize("kind", ["compile", "best_practice"])
@pytest.mark.parametrize("package_newer", [True, False])
def test_results_compare_package_and_pondnt_logs(server, tmp_path, monkeypatch, kind, package_newer):
    """An old log from either route must not mask a newer result from the other."""
    import os
    extension = "xppc" if kind == "compile" else "xppbp"
    own = tmp_path / "d365-logs" / "ContosoExt" / f"Dynamics.AX.ContosoExt.{extension}.xml"
    package = tmp_path / "package" / f"Dynamics.AX.ContosoExt.{extension}.xml"
    for path, message in ((own, "pondnt result"), (package, "package result")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DIAGNOSTICS.format(items=ITEM.format(sev="Error", n=1, msg=message)))
    older, newer = 1_700_000_000, 1_700_000_100
    os.utime(own, (older, older) if package_newer else (newer, newer))
    os.utime(package, (newer, newer) if package_newer else (older, older))
    original_call = server._d365_session.call

    def with_package(script, **kwargs):
        if "Get-D365InstalledPackage" in script:
            return str(package)
        return original_call(script, **kwargs)

    monkeypatch.setattr(server._d365_session, "call", with_package)
    is_error, data = call(server, "d365_results", {"module": "ContosoExt", "kind": kind})
    assert not is_error
    expected = package if package_newer else own
    assert data["file"] == str(expected)
    assert data["log_source"] == ("package" if package_newer else "pondnt")
    assert data["rows"][0]["message"] == ("package result" if package_newer else "pondnt result")
    assert "not proof" in data["note"]


@pytest.mark.parametrize("kind", ["compile", "best_practice"])
@pytest.mark.parametrize("have_local_log", [True, False])
@pytest.mark.parametrize("failure, code", [
    ("loading", "locked"), ("failed", "unavailable"), ("command_failed", "command_failed"),
])
def test_results_survive_package_lookup_failure(
        server, tmp_path, monkeypatch, kind, have_local_log, failure, code):
    from boring_service_kit.outcomes import Failed

    extension = "xppc" if kind == "compile" else "xppbp"
    local = tmp_path / "d365-logs" / "ContosoExt" / f"Dynamics.AX.ContosoExt.{extension}.xml"
    if have_local_log:
        local.parent.mkdir(parents=True)
        local.write_text(DIAGNOSTICS.format(items=ITEM.format(sev="Error", n=1, msg="retained result")))
    session = server._d365_session
    if failure == "command_failed":
        def fail_lookup(script, **kwargs):
            assert "Get-D365InstalledPackage" in script
            raise Failed("command_failed", f"lookup failed; Password={SECRET}")
        monkeypatch.setattr(session, "call", fail_lookup)
    else:
        monkeypatch.setattr(session, "state", {"state": failure, "since": "now"})

    started = time.monotonic()
    is_error, data = call(server, "d365_results", {"module": "ContosoExt", "kind": kind})
    assert time.monotonic() - started < 1
    if not have_local_log:
        assert is_error and data["code"] == code
        return
    assert not is_error and data["file"] == str(local)
    assert data["log_source"] == "pondnt"
    assert data["rows"][0]["message"] == "retained result"
    assert data["package_lookup_error"] == code
    assert "only pondNT logs" in data["note"] and "newer package" in data["note"]
    assert "not proof" in data["note"]


def test_results_fallback_does_not_hide_unreadable_local_log(server, tmp_path, monkeypatch):
    local = tmp_path / "d365-logs" / "ContosoExt" / "Dynamics.AX.ContosoExt.xppc.xml"
    local.parent.mkdir(parents=True)
    local.write_text("<unfinished")
    monkeypatch.setattr(server._d365_session, "state", {"state": "loading", "since": "now"})
    is_error, data = call(server, "d365_results", {"module": "ContosoExt"})
    assert is_error and data["code"] == "unreadable"


def test_environment_actions(server):
    _, data = call(server, "d365_environment", {"action": "restart", "services": "aos"})
    assert data["now"][0] == {"name": "AOSService", "status": "Running"}
    _, data = call(server, "d365_environment", {"action": "start"})
    assert len(data["now"]) == 2
    for args in ({"action": "reboot"}, {"action": "stop", "services": "all,aos"}, {"action": "stop", "services": "web"}):
        is_error, data = call(server, "d365_environment", args)
        assert is_error and data["code"] == "invalid_request"


def test_a_failed_environment_action_still_says_where_things_are(server):
    is_error, data = call(server, "d365_environment", {"action": "stop", "services": "batch"})
    assert is_error and data["code"] == "command_failed"
    assert data["details"]["now"][0] == {"name": "AOSService", "status": "Running"}


def test_stopped_iis_does_not_claim_application_outage_or_prescribe_start(server):
    _, data = call(server, "d365_status")
    assert data["aos"] == "Stopped"
    assert "IIS Express" in data["note"] and "does not establish" in data["note"]
    assert "d365_environment start" not in data["note"] and "will fail" not in data["note"]


def test_command_failures_and_noise_are_masked(fake_ps, tmp_path):
    session = Session(fake_ps)
    session.start()
    session.ensure_ready(wait=15)
    from boring_service_kit.outcomes import Failed
    with pytest.raises(Failed) as failed:
        session.call("Throw-Me")
    assert failed.value.code == "command_failed"
    assert SECRET in failed.value.message  # raw here; the server masks it (next test)
    session.close()


def test_server_masks_failures(server):
    session = server._d365_session
    original = session.call
    session.call = lambda script, timeout=None: original("Throw-Me")  # every PowerShell call fails
    try:
        is_error, data = call(server, "d365_models")
    finally:
        session.call = original
    assert is_error and data["code"] == "command_failed" and "Password=***" in data["message"]
    assert all(SECRET not in line for line in data["details"]["noise"])


def test_a_hanging_call_times_out_and_the_session_restarts(fake_ps, tmp_path):
    session = Session(fake_ps, call_seconds=0.5)
    session.start()
    session.ensure_ready(wait=15)
    from boring_service_kit.outcomes import Failed
    with pytest.raises(Failed) as failed:
        session.call("Sleep-Forever")
    assert failed.value.code == "timeout"
    session.ensure_ready(wait=15)
    assert session.call("Get-D365Model")[0]["name"] == "Foundation"
    session.close()


def test_missing_module_is_refused_with_advice(fake_ps, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_PS_MODE", "no_module")
    s = ready_server(fake_ps, tmp_path)
    try:
        _, status = call(s, "d365_status")
        assert status["session"]["state"] == "no_module"
        is_error, data = call(s, "d365_models")
        assert is_error and data["code"] == "not_found" and "ask the user" in data["message"]
        is_error, data = call(s, "d365_build", {"module": "ContosoExt"})
        assert is_error and data["code"] == "not_found"
    finally:
        s._d365_close()


def test_loading_is_refused_as_locked(fake_ps, tmp_path):
    s = create_server(fake_ps, tmp_path, start_session=False)
    s._d365_session.state = {"state": "loading", "since": "now"}
    is_error, data = call(s, "d365_models")
    assert is_error and data["code"] == "locked"
    _, status = call(s, "d365_status")
    assert status["session"]["state"] == "loading"


def test_a_huge_dry_run_is_cut_and_says_so(server):
    session = server._d365_session
    original = session.call
    session.call = lambda script, timeout=None, with_host=False: ((None, ["SyncEngine.exe " + "Entity," * 80000])
                                                                  if with_host else original(script, timeout))
    try:
        _, data = call(server, "d365_db_sync", {"modules": ["ApplicationSuite"], "dry_run": True})
    finally:
        session.call = original
    assert len(data["command"]) == 4000 and data["chars"] > 500000 and data["truncated"] is True


def test_best_practice_results_show_warnings_by_default(fake_ps, tmp_path):
    folder = tmp_path / "d365-logs" / "ContosoExt" / "ContosoExt"
    folder.mkdir(parents=True)
    item = ("<Diagnostic><DiagnosticType>BestPractices</DiagnosticType><Severity>Warning</Severity>"
            "<Path>dynamics://Table/T{n}</Path><ElementType>Table</ElementType><Moniker>m</Moniker>"
            "<Message>BPError {n}</Message><ItemSpecific>field F{n}</ItemSpecific></Diagnostic>")
    (folder / "Dynamics.AX.ContosoExt.xppbp.xml").write_text(
        DIAGNOSTICS.format(items="".join(item.format(n=n) for n in range(3))))
    s = ready_server(fake_ps, tmp_path)
    try:
        _, data = call(s, "d365_results", {"module": "ContosoExt", "kind": "best_practice"})
        assert data["kind"] == "best_practice" and data["totals"] == {"Warning": 3} and data["shown"] == 3
        assert data["rows"][0]["line"] is None and data["rows"][0]["item"] == "field F0"
        _, data = call(s, "d365_results", {"module": "ContosoExt", "kind": "best_practice", "warnings": False})
        assert data["shown"] == 0
        is_error, data = call(s, "d365_results", {"module": "ContosoExt"})  # no compile results yet
        assert is_error and data["code"] == "not_found"
    finally:
        s._d365_close()


def test_job_tail_drops_clixml(tmp_path):
    from boring_pondnt.d365 import Jobs
    jobs = Jobs("unused", tmp_path)
    log = tmp_path / "x.log"
    log.write_text("#< CLIXML\n<Objs Version=\"1.1.0.1\"><Obj/></Objs>\nBuild completed\n")
    jobs._jobs["j"] = {"log": str(log)}
    assert jobs.tail("j", 10) == ["Build completed"]



SYNC_STDOUT = """=== SyncEngine Parameters ===
DatabaseConnectionString: **Intentionally omitted for security**
TableOrViewList: ContosoTable,ContosoStaging
SyncMode: PartialList
ContinueOnError: True
2026-10-05T07:43:01.1+00:00 Beginning sync step: table sync.
2026-10-05T07:43:09.2+00:00 table sync finished. Time elapsed: 00:00:08.1.
Log level - Error | Infolog diagnostic message: 'Cannot create a record in Entity (DMFEntity). Entity: Contoso, ContosoStaging.
The record already exists.' on category 'Error'.
Log level - Error | Infolog diagnostic message: 'Error synching entity metadata for entity - ContosoEntity. Message -    at Microsoft.Dynamics.Ax.MSIL.cqlCursorIL.insert(IntPtr table)
   at Dynamics.AX.Application.DMFEntity.insert()
   at Dynamics.AX.Application.DmfDataPopulation.`syncEntityMetadata(StringCollection list).' on category 'Error'.
Log level - Warning | Infolog diagnostic message: 'Index ContosoIdx rebuilt with Password=hunter2' on category 'Warning'.
2026-10-05T07:44:01.0+00:00 PartialList finished. Time elapsed: 00:01:00.0.
Sync finished and took 140210 milliseconds.
"""
SYNC_STDERR = "\n".join(line for line in SYNC_STDOUT.splitlines()[7:12]) + "\n"


def test_sync_results_turn_the_engine_logs_into_rows(fake_ps, tmp_path):
    logs = tmp_path / "d365-logs" / "sync"
    logs.mkdir(parents=True)
    (logs / "SyncEngine.exe_StdOutput.log").write_text("﻿" + SYNC_STDOUT, encoding="utf-8")
    (logs / "SyncEngine.exe_ErrOutput.log").write_text("﻿" + SYNC_STDERR, encoding="utf-8")
    s = ready_server(fake_ps, tmp_path)
    try:
        _, data = call(s, "d365_results", {"kind": "sync"})
        assert data["mode"] == "PartialList" and data["milliseconds"] == 140210
        assert data["steps"] == [{"step": "table sync", "elapsed": "00:00:08.1"},
                                 {"step": "PartialList", "elapsed": "00:01:00.0"}]
        assert data["totals"] == {"Error": 2, "Warning": 1}  # stdout and stderr repeat each other
        assert [r["message"] for r in data["rows"]] == [
            "Cannot create a record in Entity (DMFEntity). Entity: Contoso, ContosoStaging. The record already exists.",
            "Error synching entity metadata for entity - ContosoEntity."]
        assert all(r["category"] == "Error" and r["count"] == 1 for r in data["rows"])
        _, data = call(s, "d365_results", {"kind": "sync", "warnings": True})
        assert data["rows"][-1]["level"] == "Warning" and "hunter2" not in json.dumps(data)
        is_error, data = call(s, "d365_results", {"kind": "compile"})
        assert is_error and data["code"] == "invalid_request" and "module" in data["message"]
        # a new sync clears the last run's logs and writes its own into the state folder
        _, started = call(s, "d365_db_sync", {"modules": ["ContosoExt"]})
        assert not (logs / "SyncEngine.exe_StdOutput.log").exists()
        is_error, data = call(s, "d365_results", {"kind": "sync"})
        assert is_error and data["code"] == "not_found" and "d365_db_sync" in data["message"]
        assert started["kind"] == "sync"
        call(s, "d365_job", {"job_id": started["job_id"], "stop": True})
    finally:
        s._d365_close()


def test_sync_messages_end_cleanly():
    from boring_pondnt.d365_tools import _sync_entries
    lines = ["Log level - Error | Infolog diagnostic message: 'Boom without category",
             "2026-10-05T07:43:01.1+00:00 Beginning sync step: table sync.",
             "Log level - Warning | 'Lookup at CustTable(field) failed' on category 'Warning'.",
             "Log level - Error | 'Error synching entity - X. Message -    at A.B.C(Int32 x)",
             "   at D.E()",
             "   at F.`G(String s).' on category 'Error'.",
             "Sync finished and took 5 milliseconds."]
    assert _sync_entries(lines) == [("Error", "Boom without category", None),
                                    ("Warning", "Lookup at CustTable(field) failed", "Warning"),
                                    ("Error", "Error synching entity - X.", "Error")]


def test_a_second_sync_waits_for_the_first(server, tmp_path):
    logs = tmp_path / "d365-logs" / "sync"
    _, started = call(server, "d365_db_sync", {"modules": ["SlowJob"]})
    try:
        is_error, data = call(server, "d365_db_sync", {"modules": ["ContosoExt"]})
        assert is_error and data["code"] == "locked" and data["details"]["job_id"] == started["job_id"]
        (logs / "SyncEngine.exe_StdOutput.log").write_text("SyncMode: PartialList\n")
        _, data = call(server, "d365_results", {"kind": "sync"})
        assert "partial" in data["note"]
    finally:
        call(server, "d365_job", {"job_id": started["job_id"], "stop": True})


# --- d365_sql, on SQLite standing in for SQL Server (the executor only needs DB-API) ---

def sql_server(fake_ps, tmp_path):
    import sqlite3

    db = tmp_path / "axdb.sqlite"
    setup = sqlite3.connect(db)
    setup.executescript("CREATE TABLE T (ID INTEGER PRIMARY KEY, NAME TEXT, QTY NUMERIC, NOTE TEXT, RAW BLOB);"
                        "INSERT INTO T VALUES (1, 'Grüße ✓', 1.5, NULL, x'00FF');"
                        "INSERT INTO T VALUES (2, 'two', 2, 'Password=hunter2', NULL);"
                        "INSERT INTO T VALUES (3, 'three', 3, NULL, NULL);")
    setup.commit()
    setup.close()
    seen = []

    def connect_factory(database, timeout):
        seen.append((database, timeout))
        return lambda: sqlite3.connect(db)

    return ready_server(fake_ps, tmp_path, sql_connect=connect_factory), db, seen


def test_sql_reads_as_json_rows_bounded_and_masked(fake_ps, tmp_path):
    s, _, seen = sql_server(fake_ps, tmp_path)
    try:
        is_error, data = call(s, "d365_sql", {"sql": "SELECT ID, NAME, QTY, NOTE, RAW FROM T ORDER BY ID", "max_rows": 2})
        assert not is_error and data["database"] == "AxDB" and seen == [("AxDB", 60)]
        first = data["results"][0]
        assert first["columns"] == ["ID", "NAME", "QTY", "NOTE", "RAW"] and first["truncated"] is True
        assert first["rows"][0] == [1, "Grüße ✓", 1.5, None, "0x00FF"]
        assert first["rows"][1][3] == "Password=***"  # everything the d365 server returns is masked
    finally:
        s._d365_close()


def test_sql_batches_commit_one_by_one_and_stop_at_the_first_error(fake_ps, tmp_path):
    import sqlite3

    s, db, _ = sql_server(fake_ps, tmp_path)
    try:
        script = ("INSERT INTO T (ID, NAME) VALUES (10, 'ten')\ngo\n"
                  "INSERT INTO T (ID, NAME) VALUES (1, 'duplicate')\nGO -- the second batch fails\n"
                  "INSERT INTO T (ID, NAME) VALUES (11, 'never')")
        is_error, data = call(s, "d365_sql", {"sql": script})
        assert is_error and data["code"] == "sql_error" and "UNIQUE" in data["message"].upper()
        assert data["details"]["batch"] == 2 and data["details"]["batches_done"] == 1
        assert data["details"]["rolled_back"] is False
        ids = [r[0] for r in sqlite3.connect(db).execute("SELECT ID FROM T ORDER BY ID")]
        assert ids == [1, 2, 3, 10]
    finally:
        s._d365_close()


def test_sql_atomic_is_all_or_nothing(fake_ps, tmp_path):
    import sqlite3

    s, db, _ = sql_server(fake_ps, tmp_path)
    try:
        script = ("UPDATE T SET NAME = 'changed' WHERE ID = 2\nGO\n"
                  "INSERT INTO T (ID, NAME) VALUES (20, 'twenty')\nGO\n"
                  "INSERT INTO T (ID, NAME) VALUES (1, 'duplicate')")
        is_error, data = call(s, "d365_sql", {"sql": script, "atomic": True})
        assert is_error and data["details"]["rolled_back"] is True and data["details"]["batch"] == 3
        rows = sqlite3.connect(db).execute("SELECT ID, NAME FROM T ORDER BY ID").fetchall()
        assert rows == [(1, "Grüße ✓"), (2, "two"), (3, "three")]
        is_error, data = call(s, "d365_sql", {"sql": script.rsplit("GO", 1)[0], "atomic": True})
        assert not is_error and [r.get("rows_changed") for r in data["results"]] == [1, 1]
        assert sqlite3.connect(db).execute("SELECT NAME FROM T WHERE ID = 2").fetchone() == ("changed",)
    finally:
        s._d365_close()


def test_sql_refusals(fake_ps, tmp_path):
    s, _, seen = sql_server(fake_ps, tmp_path)
    try:
        for args in ({"sql": "  "}, {"sql": "SELECT 1", "database": "AxDB; DROP"}, {"sql": "SELECT 1", "timeout": 0},
                     {"sql": "SELECT 1", "max_rows": 5000}, {"sql": "GO\nGO"}):
            is_error, data = call(s, "d365_sql", args)
            assert is_error and data["code"] == "invalid_request", args
    finally:
        s._d365_close()


def test_sql_cells_and_batches():
    import datetime
    import decimal

    from boring_pondnt.sqlserver import batches, cell

    assert cell(decimal.Decimal("1.500000")) == "1.5" and cell(decimal.Decimal("100.000000")) == "100"
    assert cell(datetime.datetime(2026, 10, 5, 12, 0)) == "2026-10-05T12:00:00"
    assert cell("x" * 2500).endswith("... (2500 chars)") and cell(b"\x01" * 2000).endswith("...")
    assert batches("a\nGO\n b \n  go  \nc\nGOTO x\n") == ["a", "b", "c\nGOTO x"]


def test_sql_server_messages_are_its_own_words():
    from boring_pondnt.sqlserver import clean

    raw = ("('42S02', \"[42S02] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]Invalid object name "
           "'NOSUCHTABLE'. (208) (SQLExecDirectW)\")")
    assert clean(raw) == "Invalid object name 'NOSUCHTABLE'."
    assert clean("[Microsoft][ODBC Driver 17 for SQL Server][SQL Server]hello") == "hello"
    two = ("('23000', \"[23000] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]Violation of PRIMARY KEY "
           "constraint 'I_1'. (2627) (SQLExecDirectW); [23000] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]"
           "The statement has been terminated. (3621)\")")
    assert clean(two) == "Violation of PRIMARY KEY constraint 'I_1'. The statement has been terminated."


def test_behind_a_pond_no_call_outlasts_the_ponds_bound(fake_ps, tmp_path):
    """With call_budget (a pond section) waits, SQL and service restarts stay within it;
    without one (stdio, Copilot on loopback) the usual limits hold."""
    import sqlite3
    db = tmp_path / "axdb.sqlite"
    sqlite3.connect(db).close()
    seen = []

    def connect_factory(database, timeout):
        seen.append(timeout)
        return lambda: sqlite3.connect(db)

    s = ready_server(fake_ps, tmp_path, sql_connect=connect_factory, call_budget=28.0)
    try:
        assert s._d365_session.call_seconds == 28.0
        is_error, data = call(s, "d365_job", {"job_id": "unknown", "wait_seconds": 40})
        assert is_error and "0-28" in data["message"]
        is_error, data = call(s, "d365_environment", {"action": "restart", "services": "aos"})
        assert is_error and "job_start" in data["message"] and "Restart-D365Environment" in data["message"]
        is_error, data = call(s, "d365_sql", {"sql": "SELECT 1", "timeout": 90})
        assert is_error and "1-28" in data["message"]
        is_error, _ = call(s, "d365_sql", {"sql": "SELECT 1"})
        assert not is_error and seen == [28]
    finally:
        s._d365_close()
    loose = ready_server(fake_ps, tmp_path, sql_connect=connect_factory)
    try:
        assert loose._d365_session.call_seconds == 60
        is_error, _ = call(loose, "d365_sql", {"sql": "SELECT 1"})
        assert not is_error and seen[-1] == 60
    finally:
        loose._d365_close()
