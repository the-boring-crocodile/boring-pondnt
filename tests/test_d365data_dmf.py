"""DMF boundary tests using synthetic packages and HTTP replies."""
import asyncio
import io
import json
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from boring_service_kit.outcomes import Failed, Refused
from boring_pondnt.d365_entra import Response, urllib_transport
from boring_pondnt.d365data_client import Config, DataClient
from boring_pondnt.d365data_packages import MAX_PACKAGE_BYTES
from boring_pondnt.d365data_server import create_server
from boring_pondnt.d365data_submissions import Submissions
from test_d365data import invoke


def test_published_tool_list_budget(tmp_path):
    server = create_server(client(tmp_path), state_dir=tmp_path)
    tools = [t.model_dump(by_alias=True, exclude_none=True) for t in asyncio.run(server.list_tools())]
    assert len(json.dumps(tools, ensure_ascii=False, separators=(",", ":"))) <= 9_263  # 3,663 + 5,600


def package():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(zipfile.ZipInfo("data.csv"), b"a,b\n1,\xff\n")
    return stream.getvalue()


class Fake:
    def __init__(self):
        self.calls = []
        self.status = "Succeeded"
        self.errors = '[]'
        self.url = "https://storage.example/dmf/package.zip?sig=PRIVATE"
        self.responses = {}
        self.download = package()

    def __call__(self, url, headers, body, timeout, **options):
        self.calls.append((url, headers, body, options))
        if "login.microsoftonline.com" in url:
            return Response(200, '{"access_token":"TOKEN","expires_in":3600}')
        if url.startswith("https://storage.example"):
            assert "Authorization" not in headers
            assert options["allow_redirects"] is False
            assert options["binary"] is True
            if options["method"] == "PUT":
                assert body == package()
                assert headers["x-ms-blob-type"] == "BlockBlob"
            return self.responses.get("blob", Response(200, "", raw=self.download))
        assert headers["Authorization"] == "Bearer TOKEN"
        action = url.rsplit(".", 1)[-1]
        if action in self.responses:
            return self.responses[action]
        values = {"ExportToPackage": "export-1", "ImportFromPackage": "import-1",
                  "GetAzureWriteUrl": json.dumps({"BlobId": "id", "BlobUrl": self.url}),
                  "GetExportedPackageUrl": self.url, "GetExecutionSummaryStatus": self.status,
                  "GetExecutionErrors": self.errors}
        return Response(200, json.dumps({"value": values[action]}))


def client(tmp_path, fake=None):
    return DataClient(Config("https://aos.example", "t", "c", "s",
                            package_folder=str(tmp_path)), fake or Fake(), secret=lambda: "SECRET")


@pytest.mark.parametrize(("operation", "steps", "expected"), [
    ("export", [3, 2], [10, 7]),
    ("import", [3, 2, 3, 1], [10, 7, 5, 2]),
    ("download", [3, 2, 1, 1, 1], [10, 7, 5, 4, 3]),
])
def test_dmf_steps_share_one_deadline(tmp_path, monkeypatch, operation, steps, expected):
    now = [100.0]
    monkeypatch.setattr("boring_pondnt.d365_entra.time.monotonic", lambda: now[0])
    fake = Fake()
    spent = iter(steps)
    timeouts = []

    def transport(url, headers, body, timeout, **options):
        timeouts.append(timeout)
        now[0] += next(spent)
        return fake(url, headers, body, timeout, **options)

    data = client(tmp_path, transport)
    data.config.timeout = 10
    data.config.dmf_submit_timeout = 10
    if operation == "export":
        assert data.dmf_export("p", "usmf")["execution_id"] == "export-1"
    elif operation == "import":
        (tmp_path / "input.zip").write_bytes(package())
        assert data.dmf_import("p", "input.zip", "usmf")["execution_id"] == "import-1"
    else:
        assert data.dmf_status("id", True)["downloaded"]
    assert timeouts == expected


def test_import_deadline_after_upload_reports_uncertainty(tmp_path, monkeypatch):
    now = [100.0]
    monkeypatch.setattr("boring_pondnt.d365_entra.time.monotonic", lambda: now[0])
    fake = Fake()

    def transport(url, headers, body, timeout, **options):
        response = fake(url, headers, body, timeout, **options)
        if options.get("method") == "PUT":
            now[0] += timeout
        return response

    (tmp_path / "input.zip").write_bytes(package())
    with pytest.raises(Failed) as error:
        client(tmp_path, transport).dmf_import("p", "input.zip", "usmf")
    assert error.value.code == "timeout"
    assert len(error.value.details["upload_name"]) == 36
    assert "may have been submitted" in error.value.details["note"]
    assert "history" in error.value.details["note"]
    assert not any("ImportFromPackage" in call[0] for call in fake.calls)


IMPORT = {"definition_group": "p", "package_path": "input.zip", "company": "usmf"}


def test_fast_server_import_keeps_existing_result(tmp_path):
    fake = Fake()
    (tmp_path / "input.zip").write_bytes(package())
    data = client(tmp_path, fake)
    assert data.config.dmf_submit_timeout == 600
    error, result = invoke(create_server(data, tmp_path / "state"), "dmf_import", IMPORT)
    assert not error and set(result) == {"execution_id", "upload_name", "package_bytes"}
    assert result["execution_id"] == "import-1" and result["package_bytes"] == len(package())


@pytest.mark.parametrize("bad", [0, -1, False, "600", float("inf"), float("nan")])
def test_submission_timeout_must_be_positive_and_finite(bad):
    with pytest.raises(ValueError, match="dmf_submit_timeout"):
        Config("https://aos.example", "t", "c", "s", dmf_submit_timeout=bad)


def test_server_import_can_be_polled_and_refuses_concurrent_imports(tmp_path):
    started, release = threading.Event(), threading.Event()
    fake = Fake()
    timeouts = []

    def transport(url, headers, body, timeout, **options):
        timeouts.append(timeout)
        if url.endswith(".ImportFromPackage"):
            started.set()
            assert release.wait(5)
        return fake(url, headers, body, timeout, **options)

    (tmp_path / "input.zip").write_bytes(package())
    data = client(tmp_path, transport)
    data.config.dmf_submit_timeout = 40
    state = tmp_path / "state"
    server = create_server(data, state, call_budget=5)
    error, pending = invoke(server, "dmf_import", IMPORT)
    assert not error and pending["state"] == "submitting" and pending["package_bytes"] == len(package())
    submission_id = pending["submission_id"]
    args = {"submission_id": submission_id}
    try:
        assert started.wait(5)
        assert all(25 < timeout <= 40 for timeout in timeouts)
        assert invoke(server, "dmf_status", args) == (False, pending)
        error, busy = invoke(server, "dmf_import", IMPORT)
        assert error and busy["code"] == "busy" and busy["details"]["submission_id"] == submission_id
        assert submission_id in busy["message"]
        restarted = create_server(client(tmp_path), state)
        error, unknown = invoke(restarted, "dmf_status", args)
        assert not error and unknown["state"] == "unknown" and "execution history" in unknown["note"]
    finally:
        release.set()
    result = poll_submission(server, args)
    assert result["state"] == "submitted" and result["execution_id"] == "import-1"
    assert result["status"] == "Succeeded" and result["errors"] == []
    assert sum(call[0].endswith(".ImportFromPackage") for call in fake.calls) == 1


def test_package_is_read_outside_the_submission_lock_and_rechecked_under_it(tmp_path):
    (tmp_path / "input.zip").write_bytes(package())
    fake = Fake()
    submitting, finish = threading.Event(), threading.Event()

    def transport(url, headers, body, timeout, **options):
        if url.endswith(".ImportFromPackage"):
            submitting.set()
            assert finish.wait(5)
        return fake(url, headers, body, timeout, **options)

    data = client(tmp_path, transport)
    reading, release = threading.Event(), threading.Event()
    prepare = data.prepare_import

    def slow_prepare(*args):
        if threading.current_thread().name == "slow":
            reading.set()
            assert release.wait(5)
        return prepare(*args)

    data.prepare_import = slow_prepare
    submissions = Submissions(data, tmp_path / "state" / "dmf-submissions.json")
    outcome = {}

    def slow_start():
        try:
            outcome["result"] = submissions.start("p", "input.zip", "usmf", True, True, 5)
        except Refused as error:
            outcome["error"] = error

    worker = threading.Thread(target=slow_start, name="slow")
    worker.start()
    try:
        assert reading.wait(5)
        # Status answers while a package is being read.
        polled = threading.Thread(target=pytest.raises, args=(Refused, submissions.status, "a" * 32))
        polled.start()
        polled.join(2)
        assert not polled.is_alive(), "status waited for a package read"
        # A second import passes while the first is still reading, and takes the slot.
        pending = submissions.start("p", "input.zip", "usmf", True, True, 0)
        assert pending["state"] == "submitting"
        assert submitting.wait(5)
        release.set()
        worker.join(5)
        assert outcome["error"].code == "busy"  # refused by the check under the lock
    finally:
        release.set()
        finish.set()
        worker.join(5)
    from time import monotonic, sleep
    deadline = monotonic() + 5
    while submissions.status(pending["submission_id"])[0]["state"] == "submitting" and monotonic() < deadline:
        sleep(0.01)
    assert submissions.status(pending["submission_id"])[0]["execution_id"] == "import-1"
    assert sum(call[0].endswith(".ImportFromPackage") for call in fake.calls) == 1


def poll_submission(server, args):
    from time import monotonic, sleep
    deadline = monotonic() + 5
    while monotonic() < deadline:
        error, result = invoke(server, "dmf_status", args)
        assert not error, result
        if result["state"] != "submitting":
            return result
        sleep(0.01)
    pytest.fail("submission did not settle")


def test_background_import_failure_retains_upload_and_masks_diagnostics(tmp_path):
    release = threading.Event()
    fake = Fake()
    fake.responses["ImportFromPackage"] = Response(400, json.dumps(
        {"error": {"message": "Cannot import " + fake.url + " Bearer PRIVATE"}}))

    def transport(url, headers, body, timeout, **options):
        if url.endswith(".ImportFromPackage"):
            assert release.wait(5)
        return fake(url, headers, body, timeout, **options)

    (tmp_path / "input.zip").write_bytes(package())
    server = create_server(client(tmp_path, transport), tmp_path / "state", call_budget=5)
    try:
        error, pending = invoke(server, "dmf_import", IMPORT)
        assert not error and pending["state"] == "submitting"
    finally:
        release.set()
    result = poll_submission(server, {"submission_id": pending["submission_id"]})
    assert result["state"] == "failed" and result["code"] == "d365_error"
    assert len(result["upload_name"]) == 36 and "execution history" in result["note"]
    assert "PRIVATE" not in json.dumps(result)
    assert sum(call[0].endswith(".ImportFromPackage") for call in fake.calls) == 1


def test_submission_receipts_are_bounded_and_never_replayed(tmp_path):
    (tmp_path / "input.zip").write_bytes(package())
    fake = Fake()
    state = tmp_path / "state"
    server = create_server(client(tmp_path, fake), state)
    for _ in range(51):
        error, result = invoke(server, "dmf_import", IMPORT)
        assert not error and result["execution_id"] == "import-1"
        if _ == 0:
            oldest, = json.loads((state / "dmf-submissions.json").read_text())
    ids = json.loads((state / "dmf-submissions.json").read_text())
    assert len(ids) == len(set(ids)) == 50 and oldest not in ids
    assert all(len(i) == 32 for i in ids)
    after_restart = Fake()
    restarted = create_server(client(tmp_path, after_restart), state)
    error, result = invoke(restarted, "dmf_status", {"submission_id": oldest})
    assert error and result["code"] == "not_found"
    error, result = invoke(restarted, "dmf_status", {"submission_id": ids[-1]})
    assert not error and result["state"] == "unknown" and not after_restart.calls


@pytest.mark.parametrize("args", [{}, {"execution_id": "id", "submission_id": "a" * 32},
                                   {"submission_id": "bad"}, {"submission_id": "a" * 32, "max_chars": 0}])
def test_status_refuses_invalid_submission_arguments(tmp_path, args):
    fake = Fake()
    error, result = invoke(create_server(client(tmp_path, fake), tmp_path), "dmf_status", args)
    assert error and result["code"] == "invalid_request" and not fake.calls


def test_export_body_and_nested_reply(tmp_path):
    fake = Fake()
    fake.responses["ExportToPackage"] = Response(200, '{"value":{"value":"export-2"}}')
    assert client(tmp_path, fake).dmf_export("Project", "usmf") == {"execution_id": "export-2"}
    assert json.loads(fake.calls[-1][2]) == {"definitionGroupId": "Project", "packageName": "Project",
        "executionId": "", "reExecute": False, "legalEntityId": "usmf"}


def test_import_upload_and_submit(tmp_path):
    fake = Fake()
    (tmp_path / "input.zip").write_bytes(package())
    result = client(tmp_path, fake).dmf_import("Project", "input.zip", "usmf", False, False)
    assert result["execution_id"] == "import-1"
    assert result["package_bytes"] == len(package())
    assert len(result["upload_name"]) == 36
    body = json.loads(fake.calls[-1][2])
    assert body == {"packageUrl": fake.url, "definitionGroupId": "Project", "executionId": "",
                    "execute": False, "overwrite": False, "legalEntityId": "usmf"}
    assert "PRIVATE" not in json.dumps(result)


def test_import_without_execution_explains_there_is_nothing_to_poll(tmp_path):
    (tmp_path / "input.zip").write_bytes(package())
    fake = Fake()
    error, result = invoke(create_server(client(tmp_path, fake), tmp_path / "state"),
                           "dmf_import", {**IMPORT, "execute": False})
    assert not error and result["execution_id"] == "import-1"
    assert result["note"] == (
        "D365 attached the package to the project; no execution runs, so there is nothing to poll. "
        "Run the project in D365, or import again with execute=true.")
    assert not any("GetExecutionSummaryStatus" in call[0] for call in fake.calls)


def test_download_for_an_unexecuted_submission_says_nothing_was_attempted(tmp_path):
    (tmp_path / "input.zip").write_bytes(package())
    fake = Fake()
    server = create_server(client(tmp_path, fake), tmp_path / "state")
    error, result = invoke(server, "dmf_import", {**IMPORT, "execute": False})
    assert not error
    submission_id = json.loads((tmp_path / "state" / "dmf-submissions.json").read_text())[-1]
    error, result = invoke(server, "dmf_status", {"submission_id": submission_id, "download": True})
    assert not error and result["state"] == "submitted" and "execute=false" in result["download_note"]
    error, result = invoke(server, "dmf_status", {"submission_id": submission_id})
    assert not error and "download_note" not in result
    assert not any("GetExecutionSummaryStatus" in call[0] or "GetExportedPackageUrl" in call[0]
                   for call in fake.calls)


def test_second_concurrent_import_is_busy(tmp_path):
    fake = Fake()
    started, release = threading.Event(), threading.Event()

    def transport(url, headers, body, timeout, **options):
        if url.endswith(".ImportFromPackage"):
            started.set()
            assert release.wait(5), "first import was not released"
        return fake(url, headers, body, timeout, **options)

    (tmp_path / "input.zip").write_bytes(package())
    data = client(tmp_path, transport)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(data.dmf_import, "p", "input.zip", "usmf")
        try:
            assert started.wait(5), "first import did not reach submission"
            before = len(fake.calls)
            with pytest.raises(Refused) as error:
                data.dmf_import("p", "input.zip", "usmf")
            assert error.value.code == "busy"
            assert len(fake.calls) == before
        finally:
            release.set()
        assert first.result(timeout=5)["execution_id"] == "import-1"


def test_import_submit_failure_keeps_upload_name_and_retry_note(tmp_path):
    fake = Fake()
    fake.responses["ImportFromPackage"] = Response(400, json.dumps(
        {"error": {"message": "Cannot import " + fake.url}}))
    (tmp_path / "input.zip").write_bytes(package())
    with pytest.raises(Failed) as error:
        client(tmp_path, fake).dmf_import("p", "input.zip", "usmf")
    assert error.value.code == "d365_error"
    lookup = next(call for call in fake.calls if call[0].endswith(".GetAzureWriteUrl"))
    assert error.value.details["upload_name"] == json.loads(lookup[2])["uniqueFileName"]
    note = error.value.details["note"]
    assert "may have been submitted" in note and "execution history" in note and "before retrying" in note
    assert "PRIVATE" not in str(error.value.as_dict())
    assert sum(call[0].endswith(".ImportFromPackage") for call in fake.calls) == 1


@pytest.mark.parametrize("swap", ["outside", "other_file"])
def test_import_refuses_a_path_swapped_between_check_and_open(tmp_path, monkeypatch, swap):
    import os
    from boring_pondnt import d365data_packages
    folder = tmp_path / "packages"
    folder.mkdir()
    (folder / "input.zip").write_bytes(package())
    (folder / "other.zip").write_bytes(package())
    outside = tmp_path / "outside.zip"
    outside.write_bytes(package())
    real = os.path.realpath
    # Simulate an ancestor replaced by a junction after the lstat walk: the opened
    # path now resolves elsewhere than the checked one.
    swapped = {"outside": outside, "other_file": folder / "other.zip"}[swap]
    monkeypatch.setattr(d365data_packages.os.path, "realpath",
                        lambda p: str(swapped) if str(p).endswith("input.zip") else real(p))
    fake = Fake()
    with pytest.raises(Refused) as error:
        client(folder, fake).dmf_import("Project", "input.zip", "usmf")
    assert error.value.code == "forbidden" and not fake.calls
    monkeypatch.setattr(d365data_packages.os.path, "realpath", real)
    assert client(folder, Fake()).prepare_import("Project", "input.zip", "usmf", True, True) == package()


@pytest.mark.parametrize("name", ["../out.zip", "sub/../input.zip", "input.zip:stream", "no.zip"])
def test_import_path_refused_before_network(tmp_path, name):
    fake = Fake()
    with pytest.raises(Refused):
        client(tmp_path, fake).dmf_import("Project", name, "usmf")
    assert not fake.calls


def test_import_links_and_size_refused_before_network(tmp_path):
    fake = Fake()
    original = tmp_path / "real.zip"
    original.write_bytes(package())
    link = tmp_path / "link.zip"
    try:
        link.symlink_to(original)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(Refused):
        client(tmp_path, fake).dmf_import("Project", str(link), "usmf")
    sub = tmp_path / "linked"
    sub.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(Refused):
        client(tmp_path, fake).dmf_import("Project", str(sub / "real.zip"), "usmf")
    with original.open("wb") as stream:
        stream.truncate(MAX_PACKAGE_BYTES + 1)
    with pytest.raises(Refused):
        client(tmp_path, fake).dmf_import("Project", "real.zip", "usmf")
    assert not fake.calls


@pytest.mark.parametrize("status", ["Succeeded", "PartiallySucceeded"])
def test_status_download_binary_and_never_uses_id_as_filename(tmp_path, status):
    fake = Fake()
    fake.status = status
    result = client(tmp_path, fake).dmf_status("../execution", True)
    from pathlib import Path
    path = Path(result["package_path"])
    assert path.parent == tmp_path and path.read_bytes() == package()
    assert result["status"] == status
    assert result["errors"] == [] and result["terminal"] and result["downloaded"]
    assert result["package_bytes"] == len(package())
    assert "PRIVATE" not in json.dumps(result)


def test_empty_export_url_reports_download_note(tmp_path):
    fake = Fake()
    fake.url = ""
    result = client(tmp_path, fake).dmf_status("id", True)
    assert result["status"] == "Succeeded" and not result["downloaded"]
    assert result["download_note"] == "No exported package is available for this execution"
    assert "download_code" not in result and not list(tmp_path.iterdir())
    assert not any(call[3].get("binary") for call in fake.calls)


@pytest.mark.parametrize("status,terminal", [("Unknown",False),("NotRun",False),("Executing",False),
    ("Succeeded",True),("PartiallySucceeded",True),("Failed",True),("Canceled",True)])
def test_status_mapping(tmp_path, status, terminal):
    fake = Fake()
    fake.status = status
    result = client(tmp_path, fake).dmf_status("id")
    assert result["status"] == status and result["terminal"] is terminal
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("phase,code", [("url", "d365_error"), ("storage", "storage_error"),
                                         ("save", "package_io")])
def test_download_failure_keeps_status_and_errors(tmp_path, monkeypatch, phase, code):
    fake = Fake()
    fake.errors = json.dumps([{"ErrorMessage": "Inspect https://storage.example/no-query"}])
    if phase == "url":
        fake.responses["GetExportedPackageUrl"] = Response(400, json.dumps(
            {"error": {"message": "Download failed: https://storage.example/pkg?sig=PRIVATE"}}))
    elif phase == "storage":
        fake.responses["blob"] = Response(403, "private storage body")
    else:
        def save(*args):
            raise Failed("package_io", "cannot save: Bearer PRIVATE")
        monkeypatch.setattr("boring_pondnt.d365data_client.save_package", save)
    error, result = invoke(create_server(client(tmp_path, fake), tmp_path), "dmf_status",
                           {"execution_id": "id", "download": True})
    assert not error and result["status"] == "Succeeded" and result["terminal"]
    assert result["errors"] == [{"ErrorMessage": "Inspect [URL omitted]"}]
    assert not result["downloaded"] and result["download_code"] == code
    assert result["download_error"] and "PRIVATE" not in result["download_error"]
    assert "storage.example" not in result["download_error"]


@pytest.mark.parametrize("message,code", [
    ("Execution details were not found for execution id ExportPackage-11/4/2026 01:00:00 pm", "not_found"),
    ("Invalid execution id format", "d365_error"),
])
def test_dmf_status_maps_only_the_missing_execution_reply(tmp_path, message, code):
    fake = Fake()
    fake.responses["GetExecutionSummaryStatus"] = Response(400, json.dumps({
        "error": {"message": "An error has occurred.", "innererror": {"message": message}}
    }))
    execution_id = "ExportPackage-11/4/2026 01:00:00 pm"
    error, result = invoke(create_server(client(tmp_path, fake), tmp_path), "dmf_status",
                           {"execution_id": execution_id})
    assert error and result["code"] == code
    if code == "not_found":
        assert result["status"] == "refused" and result["details"]["execution_id"] == execution_id
    assert not any(call[0].endswith(".GetExecutionErrors") for call in fake.calls)


def test_pending_download_and_invalid_status(tmp_path):
    fake = Fake()
    fake.status = "Executing"
    assert not client(tmp_path, fake).dmf_status("id", True)["downloaded"]
    assert not any("GetExportedPackageUrl" in c[0] for c in fake.calls)
    fake.status = "new-state"
    with pytest.raises(Failed, match="unrecognized"):
        client(tmp_path, fake).dmf_status("id")


def test_error_bounds_and_unavailable_are_not_empty_success(tmp_path):
    fake = Fake()
    fake.errors = json.dumps([{"ErrorMessage": "bad"}] * 510)
    result = client(tmp_path, fake).dmf_status("id")
    assert result["errors_shown"] == 500 and result["errors_truncated"]
    assert "error limit is 500" in result["errors_note"]
    fake.responses["GetExecutionErrors"] = Response(500, "")
    result = client(tmp_path, fake).dmf_status("id")
    assert result["status"] == "Succeeded" and result["terminal"]
    assert not result["errors_available"] and result["errors_code"] == "d365_error"
    assert "HTTP 500" in result["errors_message"] and "errors" not in result


@pytest.mark.parametrize("status_code", [404, 405, 501])
def test_unavailable_error_endpoint_reports_note(tmp_path, status_code):
    fake = Fake()
    fake.responses["GetExecutionErrors"] = Response(status_code, "")
    result = client(tmp_path, fake).dmf_status("id")
    assert result["status"] == "Succeeded" and result["terminal"]
    assert not result["errors_available"] and "errors" not in result
    assert result["errors_code"] == "d365_error"
    assert result["errors_note"] == "GetExecutionErrors is unavailable on this endpoint"


def test_status_tool_budget_limits_errors_and_keeps_status(tmp_path):
    fake = Fake()
    fake.errors = json.dumps([{"ErrorMessage": "a" * 16_384}] * 3)
    server = create_server(client(tmp_path, fake), tmp_path)
    for options, count in [({}, 1), ({"max_chars": 262_144}, 3)]:
        error, result = invoke(server, "dmf_status", {"execution_id": "id", **options})
        assert not error and result["status"] == "Succeeded" and result["terminal"]
        assert result["errors_shown"] == count
        assert result["errors_truncated"] is (count < 3)
        if count < 3:
            assert "first 1 of 3" in result["errors_note"] and "raise max_chars" in result["errors_note"]
        else:
            assert "errors_note" not in result


@pytest.mark.parametrize("available", [True, False])
def test_status_budget_limits_error_text_by_compact_json_size(tmp_path, available):
    fake = Fake()
    fake.errors = "broken " + "ä" * 100
    if not available:
        fake.responses["GetExecutionErrors"] = Response(400, json.dumps({"error": {"message": fake.errors}}))
    result = client(tmp_path, fake).dmf_status("id", max_chars=60)
    assert result["status"] == "Succeeded" and result["errors_available"] is available
    field = "errors_text" if available else "errors_message"
    assert result["errors_truncated"] and len(json.dumps(result[field], ensure_ascii=False)) == 60
    assert result[field].startswith("broken " if available else "D365 returned HTTP 400")


def test_storage_failures_never_submit_or_expose_sas(tmp_path):
    fake = Fake()
    (tmp_path / "input.zip").write_bytes(package())
    fake.responses["blob"] = Response(403, "PRIVATE")
    with pytest.raises(Failed) as error:
        client(tmp_path, fake).dmf_import("p", "input.zip", "usmf")
    assert error.value.code == "storage_error"
    assert "PRIVATE" not in str(error.value.as_dict())
    assert not any("ImportFromPackage" in c[0] for c in fake.calls)
    fake.responses["blob"] = Response(200, "", truncated=True, raw=b"x")
    result = client(tmp_path, fake).dmf_status("id", True)
    assert not result["downloaded"] and result["download_code"] == "limit"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["input.zip"]


@pytest.mark.parametrize("url", ["http://remote.example/x?sig=PRIVATE", "http://127.0.0.1.evil.example/x", "http://localhost/x", "file:///x",
                                "https://user:pass@storage.example/x", "https://storage.example/x#frag"])
def test_bad_storage_url_never_dispatched(tmp_path, url):
    fake = Fake()
    fake.url = url
    result = client(tmp_path, fake).dmf_status("id", True)
    assert not result["downloaded"] and result["download_code"] == "bad_reply"
    assert not any(c[0] == url for c in fake.calls)


def test_reader_can_inspect_but_not_download_or_submit(monkeypatch, tmp_path):
    from boring_service_kit import access
    from mcp.server.auth.settings import AuthSettings
    monkeypatch.setattr(access, "calling_caller", lambda: access.Caller("issuer", "a", "a", "reader"))
    fake = Fake()
    server = create_server(client(tmp_path, fake), tmp_path, token_verifier=object(),
        auth=AuthSettings(issuer_url="https://issuer.example", resource_server_url="https://service.example",
                          validate_token_resource=True))
    for name,args in [("dmf_status", {"execution_id":"id","download":True}),
                      ("dmf_status", {"submission_id":"a"*32,"download":True}),
                      ("dmf_export", {"definition_group":"p","company":"usmf"}),
                      ("dmf_import", {"definition_group":"p","company":"usmf","package_path":"input.zip"})]:
        error,result = invoke(server,name,args)
        assert error and result["code"] == "forbidden"
    assert not fake.calls
    fake.errors = json.dumps([{"ErrorMessage":"Bearer HIDDEN"}])
    error,result = invoke(server,"dmf_status",{"execution_id":"id"})
    assert not error and "HIDDEN" not in json.dumps(result)
    monkeypatch.setattr(access, "calling_caller", lambda: access.Caller("issuer", "a", "a", "writer"))
    error,result = invoke(server,"dmf_status",{"execution_id":"id","download":True})
    assert not error and result["downloaded"]
    assert next(t for t in asyncio.run(server.list_tools()) if t.name == "dmf_status").annotations.read_only_hint is False


def test_dmf_error_omits_full_url_and_reports_bad_download(tmp_path):
    fake = Fake()
    fake.responses["ExportToPackage"] = Response(400, json.dumps(
        {"error":{"message":"failed https://storage.example/pkg?sig=PRIVATE other"}}))
    with pytest.raises(Failed) as error:
        client(tmp_path, fake).dmf_export("p", "usmf")
    assert "PRIVATE" not in error.value.message and "storage.example" not in error.value.message
    fake.responses["blob"] = Response(200, "", raw=b"not a zip")
    result = client(tmp_path, fake).dmf_status("id", True)
    assert not result["downloaded"] and result["download_code"] == "bad_reply"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("url", ["http://127.0.0.1:10000/account/blob?sig=PRIVATE",
                                 "http://[::1]:10000/account/blob?sig=PRIVATE"])
def test_literal_loopback_emulator_preserves_url_without_bearer(tmp_path, url):
    calls = []
    def transport(target, headers, body, timeout, **options):
        calls.append((target, headers, options))
        return Response(200, "", raw=package())
    data = DataClient(Config("https://aos.example", "t", "c", "s"), transport)._blob(url, "GET")
    assert data == package()
    assert calls == [(url, {}, {"method":"GET", "max_bytes":MAX_PACKAGE_BYTES,
                               "allow_redirects":False, "binary":True})]


def test_binary_transport_preserves_zip_and_bounds(monkeypatch):
    from boring_pondnt import d365_entra
    class Stream(io.BytesIO):
        status = 200
    class Opener:
        def open(self, request, timeout):
            return Stream(package())
    monkeypatch.setattr(d365_entra.urllib.request, "build_opener", lambda *args: Opener())
    reply = urllib_transport("https://storage.example/x", {}, None, 1, method="GET",
                             binary=True, max_bytes=len(package()), allow_redirects=False)
    assert reply.raw == package() and reply.text == "" and not reply.truncated
    reply = urllib_transport("https://storage.example/x", {}, None, 1, method="GET",
                             binary=True, max_bytes=5, allow_redirects=False)
    assert reply.raw == package()[:5] and reply.truncated


@pytest.mark.parametrize("payload,expected", [
    ('[{"ErrorMessage":"first\nsecond"}]', [{"ErrorMessage": "first\nsecond"}]),
    ("", []), ("   \n\t", []),
    ('[{"ErrorMessage":"ordinary"}]', [{"ErrorMessage": "ordinary"}]),
])
def test_error_json_preserves_status(tmp_path, payload, expected):
    fake = Fake()
    fake.status = "Executing"
    fake.errors = payload
    result = client(tmp_path, fake).dmf_status("id")
    assert result["status"] == "Executing" and not result["terminal"]
    assert result["errors"] == expected and result["errors_available"]


@pytest.mark.parametrize("payload,note", [
    ('[{"ErrorMessage":"an "unescaped" quote"}]',
     "D365 returned error JSON that does not parse"),
    ('{"ErrorMessage":"object"}', "GetExecutionErrors did not return a JSON list"),
])
def test_unparseable_or_nonlist_errors_keep_status(tmp_path, payload, note):
    fake = Fake()
    fake.errors = payload
    result = client(tmp_path, fake).dmf_status("id")
    assert result["status"] == "Succeeded" and result["terminal"]
    assert result["errors_available"] and "errors" not in result
    assert result["errors_text"] == payload and not result["errors_truncated"]
    assert result["errors_note"] == note


def test_error_text_is_bounded_and_omits_urls(tmp_path):
    fake = Fake()
    fake.errors = 'broken https://storage.example/pkg?sig=PRIVATE ' + "x" * 5000
    result = client(tmp_path, fake).dmf_status("id")
    assert result["status"] == "Succeeded" and result["errors_truncated"]
    assert len(result["errors_text"]) == 4000
    assert "PRIVATE" not in result["errors_text"] and "storage.example" not in result["errors_text"]


@pytest.mark.parametrize("operation,field,limit", [
    ("export", "definition_group", 60), ("export", "package_name", 100),
    ("export", "company", 4), ("import", "definition_group", 60),
    ("import", "company", 4), ("status", "execution_id", 90),
])
def test_dmf_edt_argument_limits_before_requests(tmp_path, operation, field, limit):
    (tmp_path / "input.zip").write_bytes(package())
    fake = Fake()
    data = client(tmp_path, fake)
    args = {"definition_group": "p", "company": "usmf"}
    method = data.dmf_export
    if operation == "import":
        args["package_path"] = "input.zip"
        method = data.dmf_import
    elif operation == "status":
        args = {}
        method = data.dmf_status
    args[field] = "x" * limit
    method(**args)
    assert fake.calls
    fake.calls.clear()
    args[field] += "x"
    with pytest.raises(Refused, match=f"at most {limit}"):
        method(**args)
    assert not fake.calls


def test_locale_execution_id_round_trips_without_interpretation(tmp_path):
    execution = "ExportPackage-11/3/2026 07:21:09 am"
    fake = Fake()
    fake.responses["ExportToPackage"] = Response(200, json.dumps({"value": execution}))
    data = client(tmp_path, fake)
    assert data.dmf_export("project", "usmf") == {"execution_id": execution}
    assert data.dmf_status(execution)["execution_id"] == execution
    assert all(json.loads(c[2]) == {"executionId": execution} for c in fake.calls[-2:])


def test_executing_status_keeps_mapping_errors(tmp_path):
    fake = Fake()
    fake.status = "Executing"
    fake.errors = json.dumps([{"ErrorMessage": "No mapped fields found; regenerate the mapping"}])
    result = client(tmp_path, fake).dmf_status("id")
    assert result["status"] == "Executing" and not result["terminal"]
    assert result["errors"] == json.loads(fake.errors)


def test_save_package_exclusive_publication_and_failure_cleanup(tmp_path, monkeypatch):
    from pathlib import Path
    from boring_pondnt import d365data_packages as packages
    data = package()
    first = Path(packages.save_package(str(tmp_path), data)["package_path"])
    second = Path(packages.save_package(str(tmp_path), data)["package_path"])
    assert first != second and first.read_bytes() == second.read_bytes() == data

    class ExistingId:
        hex = first.stem
    monkeypatch.setattr(packages.uuid, "uuid4", lambda: ExistingId())
    with pytest.raises(Failed, match="cannot save"):
        packages.save_package(str(tmp_path), data)
    assert first.read_bytes() == second.read_bytes() == data

    class FailedId:
        hex = "failed-write"
    monkeypatch.setattr(packages.uuid, "uuid4", lambda: FailedId())
    real_open = Path.open
    class PartialWrite:
        def __enter__(self):
            self.stream = real_open(tmp_path / "failed-write.zip", "xb")
            return self
        def write(self, value):
            self.stream.write(value[:3])
            raise OSError("simulated disk full")
        def __exit__(self, *args):
            self.stream.close()
    monkeypatch.setattr(Path, "open", lambda path, mode="r", **kw:
                        PartialWrite() if mode == "xb" else real_open(path, mode, **kw))
    with pytest.raises(Failed, match="cannot save"):
        packages.save_package(str(tmp_path), data)
    assert not (tmp_path / "failed-write.zip").exists()
    assert first.read_bytes() == second.read_bytes() == data
    assert set(tmp_path.iterdir()) == {first, second}
