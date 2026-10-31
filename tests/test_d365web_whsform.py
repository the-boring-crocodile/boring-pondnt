"""The in-form WHS app simulator read into a page, and the shared check on it (D38: d365whsapp form mode)."""

from boring_pondnt import d365web_whsform as whsform
from boring_pondnt import whs_steps

# what the whsform JS op returned on the VHD (2026-10-23), a custom flow's first page after an unknown document
S1_ERROR = {"caption": "Demo receiving", "controls": [
    {"name": "error", "role": "Label", "text": "No inbound document found", "bg": "rgb(253, 231, 233)",
     "fg": "rgb(168, 0, 0)"},
    {"name": "DemoPageS1", "role": "Label", "text": "Scan document or license plate"},
    {"name": "DemoScanId", "role": "Input", "value": "", "label": "Scan document or license plate"},
    {"name": "OK", "role": "CommandButton", "text": "OK"},
    {"name": "CancelExitProcess", "role": "CommandButton", "text": "Cancel"}]}
MENU = {"caption": "Menu", "controls": [
    {"name": "MenuLabel", "role": "Label", "text": "Main Menu"},
    {"name": "Inbound", "role": "CommandButton", "text": "Inbound"},
    {"name": "Log off", "role": "CommandButton", "text": "Log off"}]}
LOGIN = {"caption": "Login", "controls": [
    {"name": "UserId", "role": "Input", "value": ""}, {"name": "Password", "role": "Input", "value": ""},
    {"name": "Login", "role": "CommandButton", "text": "Login"}]}


def check(page, expect):
    return whs_steps.check(whsform.view_of(page), expect)


def exit_button(page):
    return whs_steps.exit_button(whsform.view_of(page).buttons)


def test_work_page():
    page = whsform.page_from_form(S1_ERROR)
    assert page["page"] == "work" and page["title"] == "Demo receiving"
    assert page["message"] == {"text": "No inbound document found"}
    assert page["fields"] == {"DemoScanId": ""} and page["buttons"] == ["OK", "CancelExitProcess"]
    assert exit_button(page) == "CancelExitProcess"
    public = whsform.public(page)
    assert not any(k.startswith("_") for k in public)


def test_menu_and_login():
    menu = whsform.page_from_form(MENU)
    assert menu["page"] == "Menu" and menu["title"] == "Main Menu" and exit_button(menu) == "Log off"
    assert whsform.page_from_form(LOGIN)["page"] == "Login"


def test_expectations_never_pass_silently():
    page = whsform.page_from_form(S1_ERROR)
    misses, skipped = check(page, {"step": "DemoPageS1", "message": "no inbound", "title": "Demo receiving",
                                   "labels": {"DemoPageS1": "scan document"}})
    assert misses == [] and skipped == []
    misses, _ = check(page, {"step": "DemoPageS3"})
    assert misses == ["step: expected 'DemoPageS3', the page has ['DemoPageS1']"]
    misses, skipped = check(page, {"step": "WHSPutaway", "page": "Default", "message_kind": "error"})
    assert misses == [] and skipped == ["page (pattern of work pages)", "step (no control named after the step)",
                                        "message_kind (the form shows info and error messages alike)"]
    misses, _ = check(whsform.page_from_form(MENU), {"page": "Login", "message": "x", "message_kind": "info"})
    assert len(misses) == 3
    misses, skipped = check(page, {"page": "Menu", "fields": {"DemoPageS1": "Scan document or license plate"}})
    assert misses == ["page: expected 'Menu', got a work page"] and skipped == []  # a label's text is its value


def test_exit_prefers_leave_over_back():
    leave = whsform.page_from_form({"caption": "Demo receiving", "controls": [
        {"name": "DemoConfirmLeave", "role": "CommandButton", "text": "Leave document"},
        {"name": "DemoBackToDocument", "role": "CommandButton", "text": "Back"}]})
    assert exit_button(leave) == "DemoConfirmLeave"


def test_step_log_lines_mask_passwords():
    login = whsform.page_from_form({"caption": "Login", "controls": [
        {"name": "UserId", "role": "Input", "value": "WHSAPP05", "label": "User ID"},
        {"name": "Password", "role": "Input", "value": "1"},
        {"name": "Login", "role": "CommandButton", "text": "Login"}]})
    text = "\n".join(whsform.describe_page(login))
    assert "User ID [UserId]: WHSAPP05" in text and "[Password]: ***" in text and ": 1" not in text
    assert whsform.describe_values({"Password": "1", "DemoScanId": "X"}, login) == "{[Password]: ***, [DemoScanId]: X}"
    work = "\n".join(whsform.describe_page(whsform.page_from_form(S1_ERROR)))
    assert work.startswith('  -> work "Demo receiving" (form)') and "message: No inbound document found" in work
