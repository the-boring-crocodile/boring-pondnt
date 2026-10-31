"""The readable WHS step log: what each worker did and saw, passwords never written."""

from __future__ import annotations

from boring_pondnt import d365whsapp_steplog
from boring_pondnt.d365whsapp_protocol import parse_page
from test_d365whsapp_client import FakeAos, sample
from test_d365whsapp_server import call, server_for


def log_text(tmp_path, worker="WHSAPP01"):
    return (tmp_path / "whs-steps" / f"steps-{worker}.log").read_text(encoding="utf-8")


def test_login_submit_and_logoff_are_logged_without_the_password(tmp_path):
    server = server_for(FakeAos("login.xml", "menu-main.xml", "menu-inbound.xml"), tmp_path)
    call(server, "whs_login", {"worker": "whsapp01", "password": "pw-never-logged"})
    error, page = call(server, "whs_submit", {"worker": "WHSAPP01", "press": "Inbound"})
    assert not error and "log_error" not in page
    text = log_text(tmp_path)
    assert "pw-never-logged" not in text
    assert "WHSAPP01  login (password not logged)" in text
    assert "  -> Menu" in text and "WHSAPP01  press Inbound" in text
    assert "buttons: " in text and "Purchase Receive" in text


def test_values_errors_and_scenario_steps(tmp_path):
    fake = FakeAos("login.xml", "menu-main.xml", "menu-inbound.xml", "poreceive-ponum.xml", "poreceive-ponum-error.xml")
    server = server_for(fake, tmp_path)
    call(server, "whs_login", {"worker": "WHSAPP01", "password": "1"})
    call(server, "whs_run_scenario", {"worker": "WHSAPP01", "steps": [
        {"press": "Inbound"}, {"press": "Purchase Receive"},
        {"values": {"PONum": "00000050"}, "press": "OK", "expect": {"step": "ItemId"}}]})
    text = log_text(tmp_path)
    assert "scenario step 1: press Inbound" in text and "scenario step 2: press Purchase Receive" in text
    step3 = text[text.index("scenario step 3:"):]
    assert "press OK, values {" in step3 and "[PONum]: 00000050}" in step3
    assert "(mismatch:" in step3 and "message error: " in step3
    assert "step PONum" in step3


def test_failed_step_is_logged_with_its_error(tmp_path):
    status, body = sample("error-tampering.txt").split(" ", 2)[1:]
    server = server_for(FakeAos("login.xml", "menu-main.xml", (int(status), body)), tmp_path)
    call(server, "whs_login", {"worker": "WHSAPP01", "password": "1"})
    error, _ = call(server, "whs_submit", {"worker": "WHSAPP01", "press": "Inbound"})
    assert error
    assert "-> error REQUEST_XML_TAMPERING" in log_text(tmp_path)


def test_password_controls_and_values_are_masked():
    page = parse_page(sample("login.xml"))
    password = next(c for c in page.controls if c.type == "password")
    password.data = "typed-secret"
    lines = "\n".join(d365whsapp_steplog.describe_page(page))
    assert "typed-secret" not in lines and "***" in lines
    shown = d365whsapp_steplog.page_values({password.name: "typed-secret", "UserId": "W1"}, page)
    assert "typed-secret" not in shown and "***" in shown and "W1" in shown


def test_whs_log_tail_and_unknown_worker(tmp_path):
    server = server_for(FakeAos("login.xml", "menu-main.xml", "menu-inbound.xml"), tmp_path)
    call(server, "whs_login", {"worker": "WHSAPP01", "password": "1"})
    call(server, "whs_submit", {"worker": "WHSAPP01", "press": "Inbound"})
    error, tail = call(server, "whs_log", {"worker": "whsapp01", "lines": 3})
    assert not error and tail["worker"] == "WHSAPP01" and len(tail["log"].splitlines()) == 3
    error, full = call(server, "whs_log", {"worker": "WHSAPP01"})
    assert "login (password not logged)" in full["log"] and "press Inbound" in full["log"]
    error, outcome = call(server, "whs_log", {"worker": "NOBODY"})
    assert error and outcome["code"] == "not_found"
    error, outcome = call(server, "whs_log", {"worker": "WHSAPP01", "lines": 0})
    assert error and outcome["code"] == "refused"


def test_size_cap_keeps_one_old_file(tmp_path, monkeypatch):
    monkeypatch.setattr(d365whsapp_steplog, "MAX_BYTES", 300)
    log = d365whsapp_steplog.StepLog(tmp_path)
    for n in range(12):
        assert log.record("w1", f"press Step{n:02d}") is None
    current, old = tmp_path / "steps-W1.log", tmp_path / "steps-W1.log.1"
    assert current.stat().st_size <= 300 and old.exists() and old.stat().st_size <= 300
    assert "Step11" in current.read_text() and not list(tmp_path.glob("*.log.2"))


def test_a_failed_write_is_reported_once_and_never_fails_the_step(tmp_path):
    blocker = tmp_path / "whs-steps"
    blocker.write_text("not a directory")  # the log directory cannot be made
    server = server_for(FakeAos("login.xml", "menu-main.xml", "menu-inbound.xml"), tmp_path)
    error, page = call(server, "whs_login", {"worker": "WHSAPP01", "password": "1"})
    assert not error and page["page"] == "Menu" and "log_error" in page
    error, page = call(server, "whs_submit", {"worker": "WHSAPP01", "press": "Inbound"})
    assert not error and page["title"] == "Inbound" and "log_error" not in page
