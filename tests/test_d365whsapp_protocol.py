"""d365whsapp protocol against pages recorded live on the d365-dev VHD (10.0.49)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from boring_pondnt.d365whsapp_protocol import (BUTTON_CLICKED, ProtocolError, build_request, escape_attribute_whitespace,
                                           parse_page, parse_service_error)

SAMPLES = Path(__file__).parent / "d365whsapp_samples"


def sample(name: str) -> str:
    return (SAMPLES / name).read_text(encoding="utf-8")


def controls_of(xml: str) -> list[dict[str, str]]:
    root = ET.fromstring(escape_attribute_whitespace(xml.split("?>", 1)[1] if xml.startswith("<?xml") else xml))
    return [dict(c.attrib) for c in root.find("Controls").findall("Control")]


def test_login_page():
    page = parse_page(sample("login.xml"))
    assert page.is_login and not page.session
    assert page.inputs() == ["UserId", "Password"]
    assert page.buttons() == ["Login"]
    assert page.control("UserId").length == 10


def test_menu_after_login():
    page = parse_page(sample("menu-main.xml"))
    assert page.pattern == "Menu" and page.session and page.user == "24"
    summary = page.summary()
    assert summary["title"] == "Main Menu"
    assert summary["buttons"][0] == "Inbound" and summary["buttons"][-1] == "Log off"
    assert summary["warehouse"] == "24"


def test_raw_tab_in_attribute_survives_the_echo():
    raw = sample("poreceive-qty-tab.xml")
    assert '"\t        HDMI' in raw  # the server writes a raw TAB inside the attribute
    # A plain parser normalizes it: the trap that caused REQUEST_XML_TAMPERING live.
    naive = ET.fromstring(raw.split("?>", 1)[1])
    naive_label = [c for c in naive.iter("Control") if c.get("name") == "RFDescription1"][0].get("label")
    assert naive_label.startswith(" ") and "\t" not in naive_label
    page = parse_page(raw)
    assert page.control("RFDescription1").label.startswith("\t")
    request = build_request(page)
    assert "&#9;" in request or "&#09;" in request
    assert controls_of(request) == controls_of(raw)


def test_echo_changes_only_values_and_the_pressed_button():
    raw = sample("poreceive-qty-tab.xml")
    page = parse_page(raw)
    request = build_request(page, {"Qty": "1", "UOM": "pcs"}, press="OK", device_id="{11111111-AAAA-4AAA-8AAA-111111111111}")
    before, after = controls_of(raw), controls_of(request)
    assert [c["name"] for c in before] == [c["name"] for c in after]
    for b, a in zip(before, after):
        changed = {k for k in set(b) | set(a) if b.get(k) != a.get(k)}
        if b["name"] == "Qty":
            assert changed == {"data"} and a["data"] == "1"
        elif b["name"] == "UOM":  # already "pcs" (the option list): unchanged
            assert not changed and a["data"] == "pcs"
        elif b["name"] == "OK":
            assert changed == {"data"} and a["data"] == BUTTON_CLICKED
        else:
            assert not changed, b["name"]
    assert "PagePattern=" in request  # the server picks the MobileApp translator by this
    assert 'DeviceId="{11111111-AAAA-4AAA-8AAA-111111111111}"' in request
    assert parse_page(raw).control("Qty").data == ""  # the page itself is untouched


@pytest.mark.parametrize("values, press, reason", [
    ({"Nope": "1"}, None, "no field 'Nope'"),
    ({"PONum": "x"}, None, "read-only"),
    ({"RFDescription1": "x"}, None, "not an input field"),
    ({"ItemId": "A" * 81}, None, "at most 80"),
    ({}, "Nope", "no button 'Nope'"),
    ({}, "Qty", "no button 'Qty'"),
])
def test_refusals_before_sending(values, press, reason):
    page = parse_page(sample("poreceive-qty-tab.xml"))
    with pytest.raises(ProtocolError, match=reason):
        build_request(page, values, press)


def test_length_limit():
    page = parse_page(sample("login.xml"))
    with pytest.raises(ProtocolError, match="at most 10"):
        build_request(page, {"UserId": "X" * 11}, "Login")


def test_messages_error_and_info():
    failed = parse_page(sample("poreceive-ponum-error.xml"))
    assert failed.message == {"kind": "error", "text": "Product cannot be received.\nPlease contact your supervisor."}
    assert failed.control("PONum").error
    done = parse_page(sample("poreceive-work-completed.xml"))
    assert done.message == {"kind": "info", "text": "Work Completed"}
    assert done.step["id"] == "ItemId"
    assert all(c["name"] != "error" for c in done.summary()["controls"])


def test_sticky_titles_are_not_reported():
    login = parse_page(sample("login-sticky.xml"))
    assert login.title == "Purchase Receive"  # what the server sends
    assert "title" not in login.summary()
    menu = parse_page(sample("menu-main-sticky.xml"))
    assert menu.summary()["title"] == "Main Menu"


def test_password_never_in_summary():
    page = parse_page(sample("login.xml"))
    request = build_request(page, {"UserId": "24", "Password": "secret"}, "Login")
    reparsed = parse_page(request)
    assert "data" not in reparsed.control("Password").summary()


@pytest.mark.parametrize("name, code, needs_login", [
    ("error-tampering.txt", "REQUEST_XML_TAMPERING", False),
    ("error-session-token.txt", "REQUEST_SESSION_TOKEN_ERROR", True),
    ("error-multi-device.txt", "MULTI_DEVICE_USER_LOGIN", True),
])
def test_service_errors(name, code, needs_login):
    status, body = sample(name).split(" ", 2)[1:]
    error = parse_service_error(int(status), body)
    assert error.code == code and error.needs_login is needs_login
    assert not error.message.startswith("An exception occured")


def test_not_a_page():
    for text in ("", "<html/>", "not xml"):
        with pytest.raises(ProtocolError):
            parse_page(text)
