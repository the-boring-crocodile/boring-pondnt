"""Shared outcomes at the boundaries changed for pondNT 0.3."""
import datetime
import decimal
import json
from pathlib import Path

import pytest
from boring_service_kit.outcomes import Failed, Refused
from boring_pondnt import disk, sqlserver
from boring_pondnt.d365data_submissions import Submissions

from test_d365data import Fake as ODataFake, make
from test_d365data_dmf import Fake, client, package


def test_relative_path_object_remains_a_refusal():
    with pytest.raises(Refused) as caught:
        disk.absolute(Path("relative.txt"))
    assert caught.value.as_data() == {
        "status": "refused", "code": "invalid_request",
        "message": "path must be absolute", "path": "relative.txt"}


def test_sql_partial_result_details_are_json_values():
    values = [float("nan"), float("inf"), float("-inf"), decimal.Decimal("1.50"),
              datetime.datetime(2026, 11, 10), b"\x01", {1}]
    rows = [[sqlserver.cell(value) for value in values]]
    outcome = Failed("sql_error", "a later batch failed", results=[{"rows": rows}])
    assert outcome.as_data()["results"][0]["rows"][0][:6] == [
        "nan", "inf", "-inf", "1.5", "2026-11-10T00:00:00", "0x01"]
    json.dumps(outcome.as_data(), allow_nan=False)


def test_expired_request_deadline_sends_no_http(monkeypatch):
    fake = ODataFake()
    data = make(fake)
    monkeypatch.setattr(data.tokens, "get", lambda **kwargs: "fixture")
    with pytest.raises(Refused) as caught:
        data._request("/data/Customers", deadline=0)
    assert caught.value.code == "not_admitted" and not fake.calls


def test_inner_admission_refusal_after_import_started_is_failed(tmp_path, monkeypatch):
    fake = Fake()
    data = client(tmp_path, fake)
    def expired(*args, **kwargs):
        raise Refused("not_admitted", "no time for storage")
    monkeypatch.setattr(data, "_blob", expired)
    with pytest.raises(Failed) as caught:
        data.submit_import("project", package(), "usmf", True, True)
    assert caught.value.code == "not_admitted"
    assert caught.value.details["effects"] == "unknown"
    assert len(caught.value.details["upload_name"]) == 36
    assert "Inspect D365" in caught.value.details["note"]
    assert any("GetAzureWriteUrl" in call[0] for call in fake.calls)
    assert not any("ImportFromPackage" in call[0] for call in fake.calls)


@pytest.mark.parametrize(("dispatch_started", "status"), [(False, "refused"), (None, "failed"), (True, "failed")])
def test_data_and_warehouse_classify_token_dispatch_evidence_identically(monkeypatch, dispatch_started, status):
    from boring_pondnt.d365_entra import EntraError
    from boring_pondnt.d365whsapp_client import Config, WhsClient, WhsError
    fake = ODataFake()
    data = make(fake)
    warehouse_calls = []
    warehouse = WhsClient(Config("https://aos.example", "tenant", "client"),
                          transport=lambda *a, **kw: warehouse_calls.append(a))
    def unavailable(**kwargs):
        raise EntraError("secret_unavailable", "cannot decrypt", dispatch_started=dispatch_started,
                         recovery="provision the credential")
    monkeypatch.setattr(data.tokens, "get", unavailable)
    monkeypatch.setattr(warehouse.tokens, "get", unavailable)
    cls = Refused if status == "refused" else Failed
    with pytest.raises(cls) as data_error:
        data._request("/data/Customers", method="POST", body=b"{}")
    with pytest.raises(WhsError) as warehouse_error:
        warehouse.call("method", {})
    assert data_error.value.as_data()["status"] == warehouse_error.value.status == status
    for error in (data_error.value, warehouse_error.value):
        assert (error.code, error.details["recovery"]) == (
            "secret_unavailable", "provision the credential")
    assert not fake.calls and not warehouse_calls


@pytest.mark.parametrize(("cls", "code", "details"), [
    (Refused, "stale", {"path": "receipt", "current_sha256": "a" * 64}),
    (Failed, "publication_uncertain", {"original": "retained-original", "replacement": "candidate"}),
    (Failed, "cleanup_uncertain", {"original": "retained-backup", "path": "receipt"}),
])
def test_submission_preserves_local_publisher_outcome_and_recovery(tmp_path, monkeypatch, cls, code, details):
    fake = Fake()
    (tmp_path / "input.zip").write_bytes(package())
    submissions = Submissions(client(tmp_path, fake), tmp_path / "state" / "dmf-submissions.json")
    def cannot_publish(*args, **kwargs):
        raise cls(code, "local publisher explanation", **details)
    monkeypatch.setattr(disk, "replace_atomically", cannot_publish)
    with pytest.raises(cls) as caught:
        submissions.start("project", "input.zip", "usmf", True, True, 0)
    assert caught.value.code == code
    assert caught.value.message.startswith("local publisher explanation")
    assert "Nothing was sent to D365." in caught.value.message
    assert caught.value.details == {**details, "d365_dispatch_started": False}
    assert not fake.calls and submissions._active is None


@pytest.mark.parametrize("stage", ["read", "write", "thread"])
def test_submission_admission_failures_send_no_request(tmp_path, monkeypatch, stage):
    fake = Fake()
    (tmp_path / "input.zip").write_bytes(package())
    receipt = tmp_path / "state" / "dmf-submissions.json"
    submissions = Submissions(client(tmp_path, fake), receipt)
    if stage == "read":
        receipt.parent.mkdir()
        receipt.write_text("broken")
    elif stage == "write":
        def unwritable(*args, **kwargs):
            raise OSError("fixture")
        monkeypatch.setattr(disk, "replace_atomically", unwritable)
    else:
        def no_thread(*args, **kwargs):
            raise RuntimeError("fixture")
        monkeypatch.setattr("boring_pondnt.d365data_submissions.threading.Thread.start", no_thread)
    with pytest.raises(Refused) as caught:
        submissions.start("project", "input.zip", "usmf", True, True, 0)
    assert caught.value.code == ("submission_start" if stage == "thread" else "submission_state")
    assert not fake.calls
