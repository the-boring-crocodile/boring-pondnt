"""The one step format, expectation check and way out, for both d365whsapp modes (D38)."""

import pytest

from boring_pondnt.whs_steps import StepError, View, check, check_steps, exit_button


def test_steps_are_one_format():
    check_steps([{"values": {"DemoScanId": "x"}, "press": "OK", "expect": {"step": "S"}, "note": "n"}])
    for bad, why in (([], "empty"), ([{"op": "press"}], "unknown keys"), ("x", "empty"), ([1], "an object"),
                     ([{"press": "OK", "expect": {"grid": {}}}], "unknown expectations"),
                     ([{"expect": ["page"]}], "must be an object")):
        with pytest.raises(StepError, match=why):
            check_steps(bad)


def test_a_view_that_sees_everything_never_skips():
    view = View(page="Default", step="ItemId", title="Purchase receive", message_text="Work completed",
                message_kind="info", fields={"PONum": "1"}, labels={"PONum": "PO number"})
    assert check(view, {"page": "Default", "step": "ItemId", "message_kind": "info", "message": "COMPLETED",
                        "fields": {"PONum": "1"}, "labels": {"PONum": "number"}}) == ([], [])
    misses, unseen = check(view, {"page": "Menu", "step": "Qty", "message_kind": "error", "fields": {"Nope": "1"}})
    assert len(misses) == 4 and unseen == []


def test_an_unseen_message_kind_still_fails_without_a_message():
    view = View(page=None, step=None, message_kind="")
    assert check(view, {"message_kind": ""}) == ([], [])
    assert check(view, {"message_kind": "error"})[0] == ["message_kind: expected 'error', got ''"]


def test_exit_order():
    assert exit_button([("OK", ""), ("Log off", "")]) == "Log off"
    assert exit_button([("Back", ""), ("CancelResetProcess", "Cancel")]) == "CancelResetProcess"
    assert exit_button([("OK", ""), ("X1", "Cancel work")]) == "X1"
    assert exit_button([("DemoBackToDocument", "Back"), ("DemoConfirmLeave", "Leave document")]) == "DemoConfirmLeave"
    assert exit_button([("OK", ""), ("Back", "")]) == "Back"
    assert exit_button([("DemoDone", "")]) == "DemoDone"  # S11 "Document fully received" (stuck logoff, 2026-10-27)
    assert exit_button([("DemoDone", "Done"), ("Back", "")]) == "DemoDone"
    assert exit_button([("DemoConfirmLeave", "Leave document"), ("DemoDone", "Done")]) == "DemoConfirmLeave"
    assert exit_button([("OK", "")]) is None


def test_an_information_page_with_one_button_is_left_by_it_only_when_it_has_no_input():
    # About (live, 2026-11-06): version, user and AOS labels and one OK button; logoff was stuck there.
    from boring_pondnt.whs_steps import exit_button
    assert exit_button([("OK", "OK")], ("Log off",)) is None  # the old answer without the page's context
    assert exit_button([("OK", "OK")], ("Log off",), acknowledge=True) == "OK"
    assert exit_button([("OK", "OK"), ("Details", "Details")], acknowledge=True) is None  # two choices: no guess
    assert exit_button([("Cancel", "Cancel"), ("OK", "OK")], acknowledge=True) == "Cancel"  # named exits first


def test_a_lone_button_that_acts_is_never_the_way_out():
    # A one-entry submenu (form mode calls only the caption "Menu" a menu) or a custom
    # "Post"/"Confirm" page: pressing it would start or post work, not leave.
    from boring_pondnt.whs_steps import exit_button
    for lone in [("Inbound", "Inbound"), ("Continue", "Continue"), ("Post", "Post"), ("Confirm", "Confirm"), ("MenuItem1", "Purchase receive")]:
        assert exit_button([lone], acknowledge=True) is None
    assert exit_button([("Close", "Close")], acknowledge=True) == "Close"
    assert exit_button([("btnOK", "Ok")], acknowledge=True) == "btnOK"


def test_a_disabled_form_button_is_no_candidate():
    from boring_pondnt.d365web_whsform import page_from_form
    page = page_from_form({"caption": "Item inquiry", "controls": [
        {"name": "Cancel", "role": "CommandButton", "text": "Cancel", "disabled": True},
        {"name": "OK", "role": "CommandButton", "text": "OK"}]})
    assert page["buttons"] == ["OK"]


def test_logoff_never_answers_a_question_with_its_ok():
    from boring_pondnt.d365whsapp_backends import _asks
    assert _asks("Complete work?", None) and _asks(None, "Do you want to continue?")
    assert not _asks("About", "Work completed")


def test_a_password_type_form_field_is_masked_whatever_its_name():
    from boring_pondnt.d365web_whsform import describe_values, page_from_form
    page = page_from_form({"caption": "Sign", "controls": [
        {"name": "Code", "role": "Input", "value": "", "password": True, "label": "Code"},
        {"name": "Item", "role": "Input", "value": "", "label": "Item"}]})
    text = describe_values({"Code": "4711", "Item": "A0001"}, page)
    assert "4711" not in text and "A0001" in text
