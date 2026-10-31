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
