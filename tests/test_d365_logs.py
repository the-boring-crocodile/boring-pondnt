"""Read-only d365_logs tool with SQL and box fakes."""
import asyncio
import os
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from boring_service_kit.outcomes import Refused
from boring_pondnt import __version__, d365_logs
from boring_pondnt.d365_server import create_server
from test_d365 import call
from test_d365_infolog import HEADER, message

NOW = datetime(2026, 11, 4, 16, tzinfo=timezone.utc)
STAMP = NOW.replace(tzinfo=None)


class Sql:
    def __init__(self, rows=(), failure=None):
        self.rows = iter(rows)
        self.failure = failure
        self.calls = []
        self.closed = False

    def factory(self, database, timeout):
        assert database == "AxDB" and 0 < timeout <= 20
        return lambda: self

    def cursor(self):
        return self

    def execute(self, query, *args):
        self.calls.append((query, args))
        if self.failure:
            raise self.failure

    def fetchone(self):
        return next(self.rows, None)

    def close(self):
        self.closed = True


def batch_row(age=0, status=4, text=None, kind=0, task="Retail discovery", raw=None, size=None):
    info = raw if raw is not None else (HEADER + message(text, kind) + b"\xff" if text is not None else b"")
    stamp = STAMP - timedelta(minutes=age)
    return (stamp, "Job caption", 126, task, 456, "usmf", status,
            "batch-user", stamp - timedelta(seconds=2), stamp, len(info) if size is None else size, info)


def batch_call(tmp_path, rows, **args):
    sql = Sql(rows)
    server = create_server(state_dir=tmp_path, start_session=False, sql_connect=sql.factory)
    error, result = call(server, "d365_logs", {"source": "batch", **args})
    assert not error, result
    assert sql.closed
    query, parameters = sql.calls[0]
    assert "BATCHHISTORY" in query and "BATCHJOBHISTORY" in query and "SELECT *" not in query
    assert query.count("?") == len(parameters) and parameters[0] == d365_logs.SCAN_ROWS + 1
    # error/warning prefilter in SQL (failed tasks or tasks with an infolog); "all" scans every task
    assert parameters[7] == (1 if args.get("level") == "all" else 0) and "b.STATUS = 3 OR DATALENGTH(b.INFO) > 0" in query
    assert "b.DATAPARTITION = r.DATAPARTITION" in query and "j.DATAPARTITION = b.DATAPARTITION" in query
    return result


def test_batch_groups_repeats_and_counts_levels_before_cuts(tmp_path):
    rows = [batch_row(0, 3, "Build broke Password=hunter2", 3),
            batch_row(5, 4, "Build broke Password=hunter2", 3),
            batch_row(7, 4, "Minor", 1),
            batch_row(8, 4, "OK", 0)]
    result = batch_call(tmp_path, rows, level="warning", limit=1)
    assert result["server"] == {"name": "d365", "version": __version__}
    assert result["totals"] == {"error": 2, "warning": 1, "info": 1, "unknown": 0}
    assert result["truncated"] and result["note"] and result["shown"] == 1
    row = result["rows"][0]
    assert row["text"] == "[error] Build broke Password=***"
    assert row["count"] == 2 and row["first"] == (NOW - timedelta(minutes=5)).isoformat()
    assert row["last"] == NOW.isoformat() and "time" not in row
    assert row["context"]["status"] == row["status"] == "Error" and row["context"]["job_id"] == 126
    assert row["status_counts"] == {"Error": 1, "Finished": 1}


def test_batch_groups_variable_job_ids_and_dmf_execution_captions(tmp_path):
    newest = batch_row(0, 3, "Batch task failed (Class Number '50784' Job Id '52665556165')", 3,
                       "Import ZZERR-2026-11-04T15:15:42-F0DDAF7D642E412987513A9216CE3585")
    older = batch_row(5, 3, "Batch task failed (Class Number '50784' Job Id '52665556166')", 3,
                      "Import ZZERR-2026-11-04T14:11:42-A0DDAF7D642E412987513A9216CE3585")
    row, = batch_call(tmp_path, [newest, older, older])["rows"]
    assert row["count"] == 3 and row["distinct"] == 2
    assert row["first"] == (NOW - timedelta(minutes=5)).isoformat() and row["last"] == NOW.isoformat()
    assert "52665556165" in row["text"] and row["context"]["task_caption"] == newest[3]


def test_normalise_group_text_keeps_short_non_process_numbers():
    text = "HTTP 400, process id was '9164', 12345, 2026-11-04T15:15:42.123+01:00, 56c736fa-b62c-4488-bbe5-84638be4d892"
    assert d365_logs.normalise_group_text(text) == "HTTP 400, process id was '<n>', <n>, <time>, <guid>"


def test_batch_status_error_without_infolog_and_case_insensitive_filter(tmp_path):
    result = batch_call(tmp_path, [batch_row(status=3), batch_row(5, 4, "OK")],
                        contains="STATUS: ERROR", group=False)
    row, = result["rows"]
    assert row["level"] == "error" and row["source"] == "batch"
    assert row["time"] == NOW.isoformat() and "count" not in row
    assert row["status"] == "Error" and "status_counts" not in row
    assert result["totals"]["error"] == 1 and result["totals"]["info"] == 0


def test_batch_error_infolog_can_belong_to_a_finished_task(tmp_path):
    rows = [batch_row(status=4, text="Import error", kind=3),
            batch_row(1, status=4, text="Import error", kind=3),
            batch_row(2, status=3, text="Import error", kind=3),
            batch_row(3, status=4, text="Import error", kind=3),
            batch_row(4, status=3, text="Import error", kind=3)]
    row, = batch_call(tmp_path, rows, level="error")["rows"]
    assert row["level"] == "error" and row["status"] == "Finished"
    assert row["status_counts"] == {"Finished": 3, "Error": 2} and row["count"] == 5


@pytest.mark.parametrize("status,code,expected", [("eRrOr", 3, "Error"),
                                                  ("finished", 4, "Finished"), ("hold", 0, "Hold")])
def test_batch_status_filter_binds_task_status_before_scan(tmp_path, status, code, expected):
    rows = [batch_row(0, 4, "Import error", 3), batch_row(1, 3, "Import error", 3),
            batch_row(2, 0, "Import error", 3)]

    class StatusSql(Sql):
        def execute(self, query, *args):
            super().execute(query, *args)
            assert "AND (? = 0 OR b.STATUS = ?)" in query.split("ORDER BY", 1)[0]
            active, task_status = args[8:10]
            assert active == 1 and task_status == code
            self.rows = (row for row in rows if row[6] == task_status)

    sql = StatusSql()
    server = create_server(state_dir=tmp_path, start_session=False, sql_connect=sql.factory)
    error, result = call(server, "d365_logs", {"source": "batch", "level": "error", "status": status})
    assert not error, result
    row, = result["rows"]
    assert row["status"] == expected and "status_counts" not in row


@pytest.mark.parametrize("source", ["process", "events", "iis", "batch"])
def test_status_refuses_unsupported_sources_and_names(tmp_path, source, monkeypatch):
    def no_io(*args, **kwargs):
        pytest.fail("invalid status must be refused before reading the source")
    server = create_server(state_dir=tmp_path, start_session=False, sql_connect=no_io)
    monkeypatch.setattr(server._d365_session, "call", no_io)
    status = "invalid" if source == "batch" else "error"
    error, result = call(server, "d365_logs", {"source": source, "status": status})
    assert error and result["code"] == "invalid_request"
    assert ("status must be one of" if source == "batch" else "status is available only for batch") in result["message"]


def test_batch_decoding_faults_keep_rows_and_byte_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(d365_logs, "SCAN_ROWS", 2)
    bad = HEADER + message("kept", 3) + b"\xfe"
    rows = [batch_row(raw=bad), batch_row(1, status=3, size=1_048_577), batch_row(2, status=3)]
    result = batch_call(tmp_path, rows, group=False, max_chars=262_144)
    assert [r["undecoded"] for r in result["rows"]] == [1, 1_048_577]
    assert result["totals"]["error"] == 2 and result["truncated"] and "scan stopped" in result["note"]
    assert f"until={(NOW - timedelta(minutes=1)).isoformat()}" in result["note"]
    cut = batch_call(tmp_path, rows[:1], max_chars=1)
    assert cut["rows"] == [] and cut["truncated"] and cut["totals"]["error"] == 1


@pytest.mark.parametrize("since,expected", [
    (None, NOW - timedelta(hours=1)), (43_200, NOW - timedelta(days=30)),
    ("2026-11-04T17:30:00+02:00", NOW - timedelta(minutes=30)),
    ("2026-11-04T15:30:00Z", NOW - timedelta(minutes=30)),
    ("2026-11-04T15:30:00", NOW - timedelta(minutes=30)),
])
def test_since_parses_minutes_or_iso_in_utc(since, expected):
    assert d365_logs.parse_since(since, NOW) == expected


@pytest.mark.parametrize("since", [0, -1, 43_201, True, "bogus", "1900-01-01T00:00:00Z"])
def test_since_refuses_invalid_or_old_times(since):
    with pytest.raises(Refused, match="since must"):
        d365_logs.parse_since(since, NOW)


def test_published_log_schema_describes_each_argument(tmp_path):
    server = create_server(state_dir=tmp_path, start_session=False)
    tool = next(t for t in asyncio.run(server.list_tools()) if t.name == "d365_logs")
    properties = tool.input_schema["properties"]
    assert tool.input_schema["required"] == ["source"]
    assert properties["source"]["enum"] == ["batch", "process", "events", "iis"]
    assert properties["level"]["enum"] == ["error", "warning", "all"]
    assert all(field["description"] for field in properties.values())
    assert "until" in tool.description
    assert "Management Reporter*" in properties["source"]["description"]
    assert "not distinct business failures" in properties["group"]["description"]
    assert "Minutes move per call" in properties["since"]["description"]
    assert len(tool.description) + sum(len(field["description"]) for field in properties.values()) < 1500


@pytest.mark.parametrize("args,field", [({"source": "all"}, "source"), ({"level": "fatal"}, "level")])
def test_logs_rejects_invalid_enums_at_tool_boundary(tmp_path, args, field):
    def no_connection(*args):
        pytest.fail("invalid requests must not connect")
    server = create_server(state_dir=tmp_path, start_session=False, sql_connect=no_connection)
    with pytest.raises(ToolError, match=f"invalid: {field}"):
        asyncio.run(server.call_tool("d365_logs", {"source": "batch", **args}))


@pytest.mark.parametrize("args", [{"since": "yesterday"}, {"limit": 501}, {"max_chars": 262_145},
                                   {"contains": "x" * 1001}])
def test_logs_refuses_invalid_arguments_before_connect(tmp_path, args):
    def no_connection(*args):
        pytest.fail("invalid requests must not connect")
    server = create_server(state_dir=tmp_path, start_session=False, sql_connect=no_connection)
    error, result = call(server, "d365_logs", {"source": "batch", **args})
    assert error and result["code"] == "invalid_request"


def test_process_log_uses_source_links_and_groups_by_message(tmp_path):
    sql = Sql([(STAMP, "Dimension mismatch 12511", 3, "Dimensions check", 10, None),
               (STAMP - timedelta(minutes=2), "Dimension mismatch 12512", 3, "Other schedule", 11, None),
               (STAMP - timedelta(minutes=4), "Other warning", 1, None, 12, None)])
    server = create_server(state_dir=tmp_path, start_session=False, sql_connect=sql.factory)
    error, result = call(server, "d365_logs", {"source": "process", "level": "warning"})
    assert not error and result["totals"] == {"error": 2, "warning": 1, "info": 0, "unknown": 0}
    repeated, warning = result["rows"]
    assert repeated["count"] == repeated["distinct"] == 2
    assert repeated["context"]["process"] == "Dimensions check" and repeated["text"] == "Dimension mismatch 12511"
    assert repeated["context"]["exception"] == "Error" and repeated["context"]["source_link_id"] == 10
    assert warning["context"]["exception"] == "Warning" and warning["context"]["process"] is None
    query, (cap, since, until, inclusive, boundary, every_level, use_job, pattern) = sql.calls[0]
    assert since < until == boundary and not inclusive and not use_job and pattern == "%%"
    assert every_level == 0 and "m.EXCEPTION <> 0" in query  # level warning: info messages filtered in SQL
    assert "l.RECID = m.PROCESSEXECUTIONSOURCELINK" in query
    assert "o.RECID = l.PROCESSSCHEDULEOCCURRENCE" in query and "SELECT *" not in query
    assert cap == d365_logs.SCAN_ROWS + 1 and since.tzinfo is None and sql.closed


def test_events_are_filtered_by_fixed_script_and_grouped_by_first_line(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=1)
    until = now + timedelta(seconds=30)
    server = create_server(state_dir=tmp_path, start_session=False)
    common = {"log": "System", "provider": "Microsoft-Windows-WAS", "event_id": 5011,
              "process": 900, "process_name": "AOSService", "level": 2}

    def reply(script, *, timeout):
        assert "-FilterXPath" in script and "@SystemTime>='$since'" in script  # UTC: no local-time shift
        assert "@SystemTime<='$until'" in script and until.strftime("%Y-%m-%dT%H:%M:%S.%f0Z") in script
        assert "$levels = @(1, 2)" in script and '"Level=$_"' in script  # default level=error
        assert "@('Application', 'System')" in script and "-MaxEvents 1001" in script
        assert "Management Reporter*" in script and "Batch\\.exe" in script
        assert "caller needle" not in script and 0 < timeout <= 20
        return {"available": True, "scan_cut": True,
                "cutoffs": [(now - timedelta(minutes=3)).isoformat(), (now - timedelta(minutes=2)).isoformat()],
                "rows": [
            {**common, "time": until.isoformat(), "message": "caller needle at the exclusive cutoff"},
            {**common, "time": (now - timedelta(minutes=2)).isoformat(), "message": "caller needle. The process id was '9164'\r\nold detail"},
            {**common, "time": now.isoformat(), "process": 9165, "message": "caller needle. The process id was '9165'\r\nPassword=hunter2"},
            {**common, "time": now.isoformat(), "message": "different", "level": 3},
        ]}

    monkeypatch.setattr(server._d365_session, "call", reply)
    error, result = call(server, "d365_logs", {"source": "events", "contains": "caller needle",
                                              "until": until.isoformat()})
    # the rows at the newest channel cut are left whole to the next page (until_skip=0 includes them)
    assert not error and result["totals"]["error"] == 1 and result["totals"]["warning"] == 0
    row, = result["rows"]
    assert row["count"] == 1 and row["last"] == row["first"] == now.isoformat()
    assert row["context"]["process"] == 9165 and row["context"]["event_id"] == 5011
    assert row["text"] == "caller needle. The process id was '9165'\r\nPassword=***"
    assert result["truncated"] and "1,000 records per channel" in result["note"]
    assert f"until={(now - timedelta(minutes=2)).isoformat()} and until_skip=0 " in result["note"]


@pytest.mark.parametrize("faulting,expected", [
    (["opushutil.exe", r"C:\Program Files\Microsoft Office\office.dll"], 0),
    ([], 0), (["w3wp.exe"], 1), (["Batch.exe"], 1),
    ([r"K:\AosService\WebRoot\bin\kernel.dll"], 1),
    ([r"J:\PackagesLocalDirectory\bin\unknown.dll"], 1),
    (["Dynamics.AX.ApplicationSuite.dll"], 1),
])
def test_application_error_requires_d365_faulting_fields(tmp_path, monkeypatch, faulting, expected):
    server = create_server(state_dir=tmp_path, start_session=False)
    def reply(script, *, timeout):
        assert "([xml]$event.ToXml()).Event.EventData.Data" in script
        assert "'AppName', 'ModuleName', 'AppPath', 'ModulePath'" in script
        assert script.index("$faulting = @()") < script.index("$cost = $message.Length")
        return {"available": True, "rows": [{
            "time": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
            "log": "Application", "provider": "Application Error", "event_id": 1000,
            "process": 900, "process_name": "", "level": 2,
            "message": "Faulting application. Other text mentions w3wp.",
            "faulting": faulting,
        }]}
    monkeypatch.setattr(server._d365_session, "call", reply)
    error, result = call(server, "d365_logs", {"source": "events"})
    assert not error and result["shown"] == expected and result["totals"]["error"] == expected


def test_events_fall_back_to_bounded_redacted_insertion_text(tmp_path, monkeypatch):
    newest = datetime.now(timezone.utc) - timedelta(minutes=1)
    server = create_server(state_dir=tmp_path, start_session=False)
    common = {"log": "Application", "provider": "Dynamics Server Azure", "event_id": 12,
              "process": 900, "process_name": "", "level": 2}

    def reply(script, *, timeout):
        assert "if ([string]::IsNullOrWhiteSpace($message))" in script
        assert "foreach ($property in $event.Properties)" in script and "[string]$property.Value" in script
        join = "$message = $insertions -join ([Environment]::NewLine)"
        empty = "$message = 'no message text and no insertion strings'"
        assert script.index(join) < script.index(empty) < script.index("$cost = $message.Length")
        assert "text_source=$textSource" in script
        return {"available": True, "rows": [
            {**common, "time": newest.isoformat(), "text_source": "insertion_strings",
             "message": "Batch 12345 failed\r\nPassword=hunter2\r\n" + "X" * d365_logs.MESSAGE_CHARS},
            {**common, "time": (newest - timedelta(seconds=1)).isoformat(), "text_source": "insertion_strings",
             "message": "Batch 12346 failed\r\nPassword=oldersecret"},
            {**common, "time": (newest - timedelta(seconds=2)).isoformat(), "text_source": "unavailable",
             "message": "no message text and no insertion strings"},
        ]}

    monkeypatch.setattr(server._d365_session, "call", reply)
    error, result = call(server, "d365_logs", {"source": "events"})
    assert not error and result["truncated"]
    repeated, empty = result["rows"]
    assert repeated["count"] == repeated["distinct"] == 2
    assert repeated["context"]["text_source"] == "insertion_strings"
    assert repeated["text"].startswith("Batch 12345 failed\r\nPassword=***")
    assert len(repeated["text"]) == d365_logs.MESSAGE_CHARS
    assert "hunter2" not in str(result) and "oldersecret" not in str(result)
    assert empty["context"]["text_source"] == "unavailable"
    assert empty["text"] == "no message text and no insertion strings"


@pytest.mark.parametrize("reply,available", [
    ({"available": False, "rows": []}, False),
    ({"available": True, "rows": []}, True),
])
def test_events_distinguishes_absence_from_no_matches(tmp_path, monkeypatch, reply, available):
    server = create_server(state_dir=tmp_path, start_session=False)
    monkeypatch.setattr(server._d365_session, "call", lambda *args, **kwargs: reply)
    error, result = call(server, "d365_logs", {"source": "events"})
    assert not error and result["available"] is available
    assert result["server"] == {"name": "d365", "version": __version__}
    if available:
        assert result["rows"] == [] and result["totals"]["error"] == 0
    else:
        assert "logs are absent" in result["reason"]


def iis_fixture(tmp_path, monkeypatch):
    from boring_pondnt import d365_box_logs

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz else STAMP

    monkeypatch.setattr(d365_logs, "datetime", Clock)
    monkeypatch.setenv("IIS_FIXTURE", str(tmp_path / "logs"))
    config = tmp_path / "applicationHost.config"
    config.write_text(
        '<configuration><system.applicationHost><sites>'
        '<siteDefaults><logFile directory="bad-default" /></siteDefaults>'
        '<site name="Other" id="2" /><site name="AOSService" id="7">'
        '<logFile directory="\u0024{IIS_FIXTURE}" /></site></sites></system.applicationHost></configuration>',
        encoding="utf-8")
    monkeypatch.setattr(d365_box_logs, "_iis_config", lambda: config)
    folder = tmp_path / "logs" / "W3SVC7"
    folder.mkdir(parents=True)
    current = folder / "u_ex261104.log"
    current.write_bytes((Path(__file__).parent / "fixtures" / "iis-w3c.log").read_bytes())
    os.utime(current, (NOW.timestamp(), NOW.timestamp()))
    old = folder / "u_ex261103.log"
    old.write_bytes(current.read_bytes())  # must not be read even if its rows would match
    prior = (NOW - timedelta(days=1)).timestamp()
    os.utime(old, (prior, prior))
    return config


def test_iis_discovers_site_groups_timings_and_omits_queries(tmp_path, monkeypatch):
    iis_fixture(tmp_path, monkeypatch)
    server = create_server(state_dir=tmp_path, start_session=False)
    error, result = call(server, "d365_logs", {"source": "iis", "contains": "/DATA/"})
    assert not error and result["totals"] == {"error": 2, "warning": 1, "info": 0, "unknown": 0}
    assert not result["truncated"]
    row, = result["rows"]
    assert row["count"] == 2 and row["first"].startswith("2026-11-04T15:41:01")
    assert row["last"].startswith("2026-11-04T15:42:00")
    assert row["context"] == {"method": "GET", "path": "/data/CustomersV3", "status": 503, "substatus": 0,
                              "time_taken": 340, "max_time_taken": 340, "avg_time_taken": 230,
                              "example_path": "/data/CustomersV3"}
    assert row["distinct"] == 1
    assert "SECRET-QUERY" not in str(result)
    error, result = call(server, "d365_logs", {"source": "iis", "level": "all", "group": False})
    assert not error and result["rows"][0]["context"]["path"] == "/health"
    assert result["rows"][1]["context"]["time_taken"] is None


@pytest.mark.parametrize("path,expected", [
    ("/data/DocumentTypes(dataAreaId='usmf',ID='ZZREG001')", "/data/DocumentTypes(…)"),
    ("/data/Customers('it''s)')/Orders(2)/Lines(3)", "/data/Customers(…)/Orders(…)/Lines(…)"),
    ("/data/AddressFormats%28AddressFormat%3D%27%D0%A0%D0%BE%D1%81%D1%81%D0%B8%D1%8F%27%29",
     "/data/AddressFormats(…)"),
    ("/data/Customers('A%2FB')/Orders(2)/Name", "/data/Customers(…)/Orders(…)/Name"),
    ("/data/Customers('open)", "/data/Customers('open)"),
    ("/data/Customers('A')extra", "/data/Customers('A')extra"),
    ("/data/Customers", "/data/Customers"),
    ("/other/Customers('A')", "/other/Customers('A')"),
    ("/data/DataManagementDefinitionGroups/Microsoft.Dynamics.DataEntities.ExportToPackage",
     "/data/DataManagementDefinitionGroups/Microsoft.Dynamics.DataEntities.ExportToPackage"),
])
def test_odata_key_path_normalisation(path, expected):
    from boring_pondnt.d365_box_logs import normalise_odata_path
    assert normalise_odata_path(path) == expected


def test_iis_groups_by_entity_set_and_preserves_newest_path(tmp_path, monkeypatch):
    iis_fixture(tmp_path, monkeypatch)
    path = tmp_path / "logs" / "W3SVC7" / "u_ex261104.log"
    older = "/data/AddressFormats(AddressFormat='Россия')/Lines(1)"
    newest = "/data/AddressFormats(AddressFormat='Россия')/Lines(2)"
    with path.open("ab") as stream:
        stream.write(f"2026-11-04 15:45:00 503 0 DELETE {older} 120\n".encode())
        stream.write(f"2026-11-04 15:46:00 503 0 DELETE {newest}?token=SECRET-QUERY 340\n".encode())
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    server = create_server(state_dir=tmp_path, start_session=False)
    args = {"source": "iis", "contains": "РОССИЯ"}
    error, result = call(server, "d365_logs", args)
    assert not error
    row, = result["rows"]
    assert row["count"] == row["distinct"] == 2
    assert row["context"]["path"] == "/data/AddressFormats(…)/Lines(…)"
    assert row["context"]["example_path"] == newest
    assert row["text"] == "DELETE /data/AddressFormats(…)/Lines(…) → HTTP 503"
    assert "SECRET-QUERY" not in str(result)
    error, result = call(server, "d365_logs", {**args, "group": False})
    assert not error and [row["context"]["path"] for row in result["rows"]] == [newest, older]
    assert "example_path" not in result["rows"][0]["context"]


IIS_LAST_LINE = b"2026-11-04 15:44:00 200 0 GET /health 1\n"


def test_iis_byte_limit_does_not_guess_missing_tail_fields(tmp_path, monkeypatch):
    from boring_pondnt import d365_box_logs
    iis_fixture(tmp_path, monkeypatch)
    # the tail is the last row alone and the head searched for #Fields holds none: no fields are known
    monkeypatch.setattr(d365_box_logs, "MAX_IIS_BYTES", len(IIS_LAST_LINE))
    monkeypatch.setattr(d365_box_logs, "MAX_IIS_HEAD_BYTES", 10)
    server = create_server(state_dir=tmp_path, start_session=False)
    error, result = call(server, "d365_logs", {"source": "iis", "level": "all"})
    assert not error and result["truncated"] and "byte limit" in result["note"] and not result["rows"]
    assert "could not be read" in result["note"]


def test_iis_byte_limit_takes_the_last_fields_header_from_the_head(tmp_path, monkeypatch):
    from boring_pondnt import d365_box_logs
    iis_fixture(tmp_path, monkeypatch)
    path = tmp_path / "logs" / "W3SVC7" / "u_ex261104.log"
    assert path.read_bytes().endswith(IIS_LAST_LINE)
    server = create_server(state_dir=tmp_path, start_session=False)
    # the head has two headers (an IIS restart); the later one describes the last row
    monkeypatch.setattr(d365_box_logs, "MAX_IIS_BYTES", len(IIS_LAST_LINE))
    error, result = call(server, "d365_logs", {"source": "iis", "level": "all", "group": False})
    assert not error and "byte limit" in result["note"] and "could not be read" not in result["note"]
    row, = result["rows"]
    assert row["context"]["path"] == "/health" and row["context"]["status"] == 200
    # a tail starting inside a line drops that partial line, and a header in the tail wins over the head's
    monkeypatch.setattr(d365_box_logs, "MAX_IIS_BYTES", len(IIS_LAST_LINE) + 100)
    error, result = call(server, "d365_logs", {"source": "iis", "level": "all", "group": False})
    assert not error and [row["context"]["path"] for row in result["rows"]] == ["/health"]
    assert "could not be read" not in result["note"]


def _cursor(note):
    match = re.search(r"until=(\S+) and until_skip=([0-9]+) ", note or "")
    return (match[1], int(match[2])) if match else None


def _pages(server, args):
    """Follow the continuation notes; return the rows of every page."""
    seen, pages = [], 0
    while True:
        error, result = call(server, "d365_logs", args)
        assert not error, result
        seen += result["rows"]
        pages += 1
        cursor = _cursor(result.get("note"))
        if cursor is None:
            return seen, pages
        assert pages < 10
        args = {**args, "until": cursor[0], "until_skip": cursor[1]}


def test_iis_paging_keeps_rows_tied_at_the_cut_second(tmp_path, monkeypatch):
    from boring_pondnt import d365_box_logs
    iis_fixture(tmp_path, monkeypatch)
    path = tmp_path / "logs" / "W3SVC7" / "u_ex261104.log"
    lines = ["#Fields: date time cs-method cs-uri-stem sc-status", "2026-11-04 15:40:00 GET /older 200"]
    lines += [f"2026-11-04 15:45:00 GET /tied{n} 200" for n in range(1, 6)]
    lines += ["2026-11-04 15:50:00 GET /newer 200"]
    path.write_text("\n".join(lines) + "\n")
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))
    monkeypatch.setattr(d365_box_logs, "MAX_IIS_ROWS", 3)
    server = create_server(state_dir=tmp_path, start_session=False)
    rows, pages = _pages(server, {"source": "iis", "level": "all", "group": False,
                                  "since": "2026-11-04T15:00:00Z", "until": "2026-11-04T16:00:00Z"})
    assert [row["context"]["path"] for row in rows] == [
        "/newer", "/tied5", "/tied4", "/tied3", "/tied2", "/tied1", "/older"] and pages == 3


class TableSql(Sql):
    """Applies the window, the inclusive boundary and TOP of the log SELECTs to fixed rows."""

    def __init__(self, rows, source):
        super().__init__()
        self.table, self.source = rows, source

    def factory(self, database, timeout):
        self.closed = False
        return super().factory(database, timeout)

    def execute(self, query, *args):
        super().execute(query, *args)
        if self.source == "batch":
            cap, since, end, _, _, inclusive, boundary = args[:7]
        else:
            cap, since, end, inclusive, boundary = args[:5]
        assert end == boundary
        match = [row for row in self.table if since <= row[0] < end or inclusive and row[0] == boundary]
        # ORDER BY time DESC, RECID DESC; the fourth or fifth column stands in for RECID
        match.sort(key=lambda row: (row[0], row[4]), reverse=True)
        self.rows = iter(match[:cap])


@pytest.mark.parametrize("source", ["batch", "process"])
def test_sql_paging_keeps_rows_tied_at_the_cut_second(tmp_path, monkeypatch, source):
    monkeypatch.setattr(d365_logs, "SCAN_ROWS", 3)
    tied = STAMP - timedelta(minutes=5)
    stamps = [STAMP - timedelta(minutes=1)] + [tied] * 5 + [STAMP - timedelta(minutes=10)]
    if source == "batch":
        table = []
        for recid, stamp in enumerate(stamps):
            row = list(batch_row(status=3, text=f"failure {recid}", kind=3))
            row[0], row[4] = stamp, 100 - recid
            table.append(tuple(row))
    else:
        table = [(stamp, f"message {recid}", 3, "Process", 100 - recid, None) for recid, stamp in enumerate(stamps)]
    sql = TableSql(table, source)
    server = create_server(state_dir=tmp_path, start_session=False, sql_connect=sql.factory)
    rows, pages = _pages(server, {"source": source, "level": "all", "group": False,
                                  "since": (NOW - timedelta(hours=1)).isoformat(), "until": NOW.isoformat()})
    texts = [row["text"].removeprefix("[error] ") for row in rows]
    expected = [f"{'failure' if source == 'batch' else 'message'} {n}" for n in range(7)]
    assert texts == expected and pages == 3
    _, last = sql.calls[-1]
    assert last[0] == d365_logs.SCAN_ROWS + 1 + 5  # TOP leaves room for the skipped rows


def test_events_continuation_includes_until_and_skips_seen_rows(tmp_path, monkeypatch):
    until = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=5)
    server = create_server(state_dir=tmp_path, start_session=False)
    common = {"log": "System", "provider": "Microsoft-Windows-WAS", "event_id": 5011,
              "process": 900, "process_name": "AOSService", "level": 2}

    def reply(script, *, timeout):
        assert (until + timedelta(microseconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%f0Z") in script
        return {"available": True, "rows": [
            {**common, "time": until.isoformat(), "message": "seen at the cut"},
            {**common, "time": until.isoformat(), "message": "not yet seen at the cut"},
            {**common, "time": (until - timedelta(minutes=1)).isoformat(), "message": "older"}]}

    monkeypatch.setattr(server._d365_session, "call", reply)
    error, result = call(server, "d365_logs", {"source": "events", "group": False, "until": until.isoformat(),
                                              "until_skip": 1, "since": 60})
    assert not error and [row["text"] for row in result["rows"]] == ["not yet seen at the cut", "older"]


@pytest.mark.parametrize("options,reason", [
    ({"until_skip": 1}, "requires an ISO until"),
    ({"until": 5, "until_skip": 1}, "requires an ISO until"),
    ({"until": "2026-11-04T15:00:00Z", "until_skip": -1}, "until_skip must be"),
])
def test_until_skip_refusals(tmp_path, options, reason):
    server = create_server(state_dir=tmp_path, start_session=False, sql_connect=lambda *args: pytest.fail("connected"))
    error, result = call(server, "d365_logs", {"source": "process", **options})
    assert error and reason in str(result)


def test_display_cut_note_does_not_suggest_contains(tmp_path):
    result = batch_call(tmp_path, [batch_row(0, 3, "first", 3), batch_row(1, 3, "second", 3)], limit=1)
    assert "Only the first 1 of 2 rows fit" in result["note"] and "contains" not in result["note"]


def test_iis_ignores_out_of_range_elapsed_time(tmp_path, monkeypatch):
    iis_fixture(tmp_path, monkeypatch)
    path = tmp_path / "logs" / "W3SVC7" / "u_ex261104.log"
    with path.open("ab") as stream:
        stream.write(b"2026-11-04 15:45:00 503 0 GET /data/CustomersV3 " + b"9" * 400 + b"\n")
    server = create_server(state_dir=tmp_path, start_session=False)
    error, result = call(server, "d365_logs", {"source": "iis"})
    assert not error and result["totals"]["error"] == 2 and result["truncated"]
    assert result["rows"][0]["context"]["avg_time_taken"] == 230


def test_iis_missing_site_is_unavailable(tmp_path, monkeypatch):
    config = iis_fixture(tmp_path, monkeypatch)
    config.write_text("<configuration><system.applicationHost><sites/></system.applicationHost></configuration>",
                      encoding="utf-8")
    server = create_server(state_dir=tmp_path, start_session=False)
    error, result = call(server, "d365_logs", {"source": "iis"})
    assert not error and result == {"available": False, "reason": "IIS site AOSService is absent",
                                    "server": {"name": "d365", "version": __version__}}


@pytest.mark.parametrize("source,field", [("batch", "job_caption"), ("process", "process")])
def test_sql_job_filter_is_bound_and_contains_searches_captions(tmp_path, source, field):
    sample = [batch_row(0, 3, "error from worker", 3)] if source == "batch" else [
        (STAMP, "error from worker", 3, "Job caption", 10, None)]
    sql = Sql(sample)
    server = create_server(state_dir=tmp_path, start_session=False, sql_connect=sql.factory)
    job = "Night's%_[~"
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=10)).replace(microsecond=0)
    error, result = call(server, "d365_logs", {"source": source, "job": job, "contains": "jOb CaPtIoN",
                                              "since": 90, "until": cutoff.isoformat()})
    assert not error and result["rows"][0]["context"][field] == "Job caption"
    query, parameters = sql.calls[0]
    assert job not in query and "COLLATE Latin1_General_100_CI_AS LIKE ? ESCAPE N'~'" in query
    assert parameters.count("%Night's~%~_~[~~%") == (2 if source == "batch" else 1)
    assert parameters[2] == cutoff.replace(tzinfo=None)
    if source == "batch":
        assert parameters[4] == parameters[2] and "j.CAPTION COLLATE" in query
        assert "b.ENDDATETIME < ?" in query and "b.CREATEDDATETIME < ?" in query
    else:
        assert "caption.process_name COLLATE" in query and "m.CREATEDDATETIME < ?" in query


def test_batch_contains_matches_task_caption(tmp_path):
    result = batch_call(tmp_path, [batch_row(status=3, task="DMF Import ZZERR")], contains="zzerr")
    assert result["rows"][0]["context"]["task_caption"] == "DMF Import ZZERR"


@pytest.mark.parametrize("source,options,reason", [
    ("events", {"job": "x"}, "only for batch and process"),
    ("iis", {"job": "x"}, "only for batch and process"),
    ("batch", {"job": "x" * 201}, "at most 200"),
    ("process", {"until": "bad"}, "until must be"),
    ("batch", {"since": 60, "until": 60}, "later than since"),
    ("batch", {"since": 60, "until": 61}, "later than since"),
])
def test_job_and_until_refusals(tmp_path, source, options, reason):
    def unexpected_connection(*args):
        pytest.fail("invalid request attempted a connection")
    server = create_server(state_dir=tmp_path, start_session=False, sql_connect=unexpected_connection)
    error, result = call(server, "d365_logs", {"source": source, **options})
    assert error and result["code"] == "invalid_request" and reason in result["message"]


def test_iis_until_filters_files_and_rows(tmp_path, monkeypatch):
    iis_fixture(tmp_path, monkeypatch)
    folder = tmp_path / "logs" / "W3SVC7"
    newer = folder / "u_ex261105.log"
    newer.write_bytes((folder / "u_ex261104.log").read_bytes())
    os.utime(newer, ((NOW + timedelta(days=1)).timestamp(),) * 2)
    server = create_server(state_dir=tmp_path, start_session=False)
    error, result = call(server, "d365_logs", {"source": "iis", "since": 60, "until": "2026-11-04T15:42:00Z"})
    assert not error and result["totals"]["error"] == 1
    row, = result["rows"]
    assert row["first"] == row["last"] == "2026-11-04T15:41:01+00:00"
    assert row["context"]["time_taken"] == 120


def test_logs_is_reader_and_reports_sql_failure(tmp_path, monkeypatch):
    from boring_service_kit import access
    from mcp.server.auth.settings import AuthSettings
    monkeypatch.setattr(access, "calling_caller", lambda: access.Caller("issuer", "r", "r", "reader"))
    sql = Sql(failure=OSError("synthetic SQL failed Password=hunter2"))
    server = create_server(state_dir=tmp_path, start_session=False, sql_connect=sql.factory, token_verifier=object(),
                           auth=AuthSettings(issuer_url="https://issuer.example",
                                             resource_server_url="https://service.example", validate_token_resource=True))
    tool = next(t for t in asyncio.run(server.list_tools()) if t.name == "d365_logs")
    assert tool.annotations.read_only_hint
    error, result = call(server, "d365_logs", {"source": "batch"})
    assert error and result["code"] == "sql_error" and "hunter2" not in result["message"]
    assert sql.closed


def test_redact_masks_token_shapes_wherever_they_stand():
    from boring_pondnt.d365_tools import redact
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4eHh4eCJ9.c2lnbmF0dXJlX19f"
    assert jwt not in redact(f"the access token was {jwt}")
    assert redact("Authorization: Bearer abcdefgh12345678") == "Authorization: Bearer ***"
    assert "Hunter2" not in redact("api_key=Hunter2 apikey: Hunter2")
    assert redact("bsk_0123456789abcdef") == "bsk_***"


def test_redact_leaves_prose_about_tokens_and_credentials_alone():
    from boring_pondnt.d365_tools import redact
    for text in ("the token expired at noon", "Invalid credentials supplied for user", "Bearer authentication failed"):
        assert redact(text) == text
    assert "Hunter2" not in redact("-Password Hunter2") and "s3cr3t" not in redact("token s3cr3t")
