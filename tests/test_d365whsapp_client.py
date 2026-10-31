"""d365whsapp client against a fake AOS that replays pages recorded on the d365-dev VHD."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from boring_pondnt import whs_steps
from boring_pondnt.d365whsapp_client import Config, Response, WhsClient, WhsError, device_id_for, view_of
from boring_pondnt.d365whsapp_protocol import parse_page


def match_expectation(page, expect):
    """The app view through the shared check: the protocol page shows everything, nothing unseen."""
    misses, unseen = whs_steps.check(view_of(page), expect)
    assert unseen == []
    return misses


def exit_button(page):
    return whs_steps.exit_button(view_of(page).buttons)


SAMPLES = Path(__file__).parent / "d365whsapp_samples"
CONFIG = Config(aos_url="https://aos.example", tenant="tenant.example", client_id="client-1", company="usmf")


def sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


class FakeAos:
    """Replies in order: a sample file name, an Exception to raise, or (status, body)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: list[dict] = []
        self.token_calls = 0

    def __call__(self, url, headers, body, timeout):
        if "login.microsoftonline.com" in url:
            self.token_calls += 1
            assert b"client_secret=s3cret" in body
            return Response(200, json.dumps({"access_token": "tok", "expires_in": 3600}))
        assert headers["Authorization"] == "Bearer tok"
        payload = json.loads(body)
        self.requests.append({"url": url, "timeout": timeout, **payload})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, tuple):
            return Response(*reply)
        return Response(200, json.dumps(sample(reply)))


def client(fake: FakeAos) -> WhsClient:
    return WhsClient(CONFIG, transport=fake, secret=lambda: "s3cret")


def test_login_sends_the_echoed_login_page():
    fake = FakeAos("login.xml", "menu-main.xml")
    c = client(fake)
    page, resumed = c.login("WHSAPP01", "1")
    assert page.pattern == "Menu" and not resumed
    first, second = fake.requests
    assert first["_xml"] == "" and first["_dataAreaId"] == "usmf"
    assert first["url"].endswith("/api/services/WHSMobileAppServices/WHSMobileAppService/getNextFormHandHeld")
    sent = parse_page(second["_xml"])
    assert sent.control("UserId").data == "WHSAPP01" and sent.control("Password").data == "1"
    assert sent.control("Login").data == "1"
    assert sent.device_id == device_id_for("WHSAPP01")
    assert fake.token_calls == 1


def test_failed_login_is_not_retried():
    fake = FakeAos("login.xml", "login.xml")
    with pytest.raises(WhsError) as raised:
        client(fake).login("WHSAPP01", "wrong")
    assert raised.value.code == "login_failed" and "not retried" in raised.value.message
    assert len(fake.requests) == 2


def test_local_refusal_sends_nothing():
    fake = FakeAos("login.xml", "menu-main.xml")
    c = client(fake)
    c.login("WHSAPP01", "1")
    with pytest.raises(WhsError) as raised:
        c.submit("WHSAPP01", {"Qty": "1"}, None)
    assert raised.value.code == "refused"
    with pytest.raises(WhsError, match="no button 'Nope'"):
        c.submit("WHSAPP01", None, "Nope")
    assert len(fake.requests) == 2


def test_submit_moves_to_the_next_page_with_a_generous_timeout():
    fake = FakeAos("login.xml", "menu-main.xml", "menu-inbound.xml")
    c = client(fake)
    c.login("whsapp01", "1")
    page, recovered = c.submit("WHSAPP01", None, "Inbound")
    assert page.summary()["title"] == "Inbound" and not recovered
    assert fake.requests[-1]["timeout"] >= 300
    assert parse_page(fake.requests[-1]["_xml"]).control("Inbound").data == "1"
    assert fake.token_calls == 1  # cached


def test_lost_reply_recovers_by_login_never_by_resend():
    fake = FakeAos("login.xml", "menu-main.xml", socket.timeout("timed out"), "login.xml", "poreceive-work-completed.xml")
    c = client(fake)
    c.login("WHSAPP01", "1")
    page, recovered = c.submit("WHSAPP01", None, "Inbound")
    assert recovered and page.message == {"kind": "info", "text": "Work Completed"}
    assert [r["_xml"] == "" for r in fake.requests] == [True, False, False, True, False]  # resend never happens


@pytest.mark.parametrize("name", ["error-session-token.txt", "error-multi-device.txt"])
def test_token_errors_recover_by_login(name):
    status, body = sample(name).split(" ", 2)[1:]
    fake = FakeAos("login.xml", "menu-main.xml", (int(status), body), "login.xml", "menu-main.xml")
    c = client(fake)
    c.login("WHSAPP01", "1")
    page, recovered = c.submit("WHSAPP01", None, "Inbound")
    assert recovered and page.pattern == "Menu"


def test_tampering_is_reported_and_the_page_kept():
    status, body = sample("error-tampering.txt").split(" ", 2)[1:]
    fake = FakeAos("login.xml", "menu-main.xml", (int(status), body), "menu-inbound.xml")
    c = client(fake)
    c.login("WHSAPP01", "1")
    with pytest.raises(WhsError) as raised:
        c.submit("WHSAPP01", None, "Inbound")
    assert raised.value.code == "REQUEST_XML_TAMPERING"
    page, _ = c.submit("WHSAPP01", None, "Inbound")  # same page still current
    assert page.summary()["title"] == "Inbound"


def test_logoff_walks_back_and_ends_the_session():
    fake = FakeAos("login.xml", "menu-main.xml", "menu-inbound.xml", "menu-main-sticky.xml", "login-sticky.xml")
    c = client(fake)
    c.login("WHSAPP01", "1")
    c.submit("WHSAPP01", None, "Inbound")
    page = c.logoff("WHSAPP01")
    assert page.is_login
    pressed = [next(ctl.name for ctl in parse_page(r["_xml"]).controls if ctl.type == "button" and ctl.data == "1")
               for r in fake.requests[-2:]]
    assert pressed == ["Back", "Log off"]
    with pytest.raises(WhsError, match="call whs_login first"):
        c.submit("WHSAPP01", None, "Inbound")


def test_no_company_anywhere_is_refused_before_any_call():
    fake = FakeAos()
    bare = WhsClient(Config(aos_url="https://aos.example", tenant="tenant.example", client_id="client-1"),
                     transport=fake, secret=lambda: "s3cret")
    with pytest.raises(WhsError) as raised:
        bare.login("WHSAPP01", "1")
    assert raised.value.code == "no_company" and fake.requests == [] and fake.token_calls == 0
    fake.replies = ["login.xml", "menu-main.xml"]
    bare.login("WHSAPP01", "1", "demf")
    assert fake.requests[0]["_dataAreaId"] == "demf"


def test_no_session():
    with pytest.raises(WhsError) as raised:
        client(FakeAos()).submit("WHSAPP09", None, "OK")
    assert raised.value.code == "no_session"


def test_device_ids_are_stable_per_worker():
    assert device_id_for("whsapp01") == device_id_for("WHSAPP01") != device_id_for("WHSAPP02")
    assert device_id_for("WHSAPP01").startswith("{") and len(device_id_for("WHSAPP01")) == 38


def test_expectations():
    page = parse_page(sample("poreceive-work-completed.xml"))
    assert match_expectation(page, {"page": "Default", "step": "ItemId", "message_kind": "info",
                                    "message": "work completed", "fields": {"PONum": "00000250"}}) == []
    misses = match_expectation(page, {"step": "Qty", "message_kind": "error", "fields": {"PONum": "1"}})
    assert len(misses) == 3


BUSY = (500, json.dumps({"Message": "An exception occured when invoking the operation - There is a process running "
                                    "triggered by this worker. Please wait a little and try to log in again or contact "
                                    "your supervisor to stop the process associated with this worker. ERROR_SESSION_BUSY",
                         "ExceptionType": "ErrorException"}))


def test_timeout_while_the_step_still_runs_says_so():
    fake = FakeAos("login.xml", "menu-main.xml", socket.timeout("timed out"), BUSY)
    c = client(fake)
    c.login("WHSAPP01", "1")
    with pytest.raises(WhsError) as raised:
        c.submit("WHSAPP01", None, "Inbound")
    assert raised.value.code == "step_running" and "whs_login" in raised.value.message
    assert "no reply within" in raised.value.message


def test_login_while_busy_says_step_running_then_resumes():
    fake = FakeAos("login.xml", "menu-main.xml", BUSY, "login.xml", "poreceive-work-completed.xml")
    c = client(fake)
    c.login("WHSAPP01", "1")
    with pytest.raises(WhsError) as raised:
        c.submit("WHSAPP01", None, "Inbound")  # an earlier request still runs
    assert raised.value.code == "step_running"
    page, resumed = c.login("WHSAPP01", "1")
    assert resumed and page.message["text"] == "Work Completed"


def test_exit_button_by_label_and_process_guide_names():
    page = parse_page(sample("menu-main.xml"))
    assert exit_button(page) == "Log off"
    assert exit_button(parse_page(sample("poreceive-ponum.xml"))) == "Cancel"
    guide = parse_page(sample("login.xml"))
    for c in guide.controls:
        if c.type == "button":
            c.name, c.label = "CancelExitProcess", "Cancel"
    assert exit_button(guide) == "CancelExitProcess"
    # Custom scan pages (S5): only OK and "Leave document"
    from boring_pondnt.d365whsapp_protocol import Control
    def button(name, label):
        return Control("button", name, label, "0", True, False, "", "", 0)
    guide.controls = [button("OK", ""), button("DemoLeaveDocument", "Leave document")]
    assert exit_button(guide) == "DemoLeaveDocument"
    guide.controls = [button("OK", "")]
    assert exit_button(guide) is None
    # its "Leave anyway?" page: confirm, do not go Back to the document (that looped)
    guide.controls = [button("DemoConfirmLeave", "Leave document"), button("DemoBackToDocument", "Back")]
    assert exit_button(guide) == "DemoConfirmLeave"
    guide.controls = [button("OK", ""), button("Back", "")]
    assert exit_button(guide) == "Back"


def test_expectation_on_label_controls():
    page = parse_page(sample("menu-main.xml"))
    label = next(c for c in page.controls if c.label)
    assert match_expectation(page, {"labels": {label.name: label.label[:4].upper()}}) == []
    assert match_expectation(page, {"labels": {label.name: "nowhere"}})[0].startswith("label ")
    assert match_expectation(page, {"labels": {"NoSuchControl": "x"}})


def test_config_with_a_bom(tmp_path):
    path = tmp_path / "d365whsapp.json"  # what PowerShell 5's Set-Content -Encoding utf8 writes
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"aos_url": "https://aos", "tenant": "t", "client_id": "c"}).encode())
    assert Config.load(path).client_id == "c"
