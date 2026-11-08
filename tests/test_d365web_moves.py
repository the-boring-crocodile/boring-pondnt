"""web_press and web_select_row say what moved, not only that they dispatched.

A press is followed by D365 going idle whether or not anything happened (a swallowed click, a
button whose command did nothing here). The client compares the page context before and after
(visible forms, the dialog in front, the active record, the pane, the scope's fields, messages,
address) and answers `changed: [...]` or `unchanged: true` with a note; it never retries. These
tests stub the page route (_js); the context op itself runs against the fake page in a browser
(test_d365web_client.py) and live on a dev box."""
import time

import pytest

from boring_pondnt.d365web_client import UNCHANGED_NOTE, Config, WebClient, context_moves

CTX = {"forms": ["CustTable"], "dialog": "", "record": {"caption": "US-001", "rows": {"Grid": "75"}},
       "pane": {"action_tab": "aptabCustomer", "tabs": [], "menu": False}, "fields": "abc"}


class Page:
    def __init__(self, url="/?mi=CustTable"):
        self.events, self.address = [], url

    def click(self, x, y):
        self.events.append(("click", x, y))

    def move(self, x, y):
        self.events.append(("move", x, y))

    def url(self):
        return self.address


def stub(monkeypatch, contexts, model=None, state=None, active=None, target=None):
    """A WebClient whose page answers `contexts` in turn for the context op."""
    web = WebClient(Config(url="https://example.test", backend="model"))
    page = Page()
    seq = list(contexts)
    calls = []

    def js(op, *args):
        calls.append(op)
        if op == "context":
            return seq.pop(0) if len(seq) > 1 else seq[0]
        if op == "model:press":
            return model
        if op == "row":
            return target or {"x": 3, "y": 4, "grid": "Grid", "row": 1, "key": "3"}
        if op == "active":
            return active if active is not None else {"error": "not_found"}
        if op == "restPoint":
            return {"error": "none"}
        return []

    monkeypatch.setattr(web, "_ensure", lambda: page)
    monkeypatch.setattr(web, "_locate", lambda *a: {"control": "Btn", "x": 1, "y": 2})
    monkeypatch.setattr(web, "_js", js)
    monkeypatch.setattr(web, "_after_step", lambda before, **kw: dict(state or {}, **kw))
    monkeypatch.setattr(time, "sleep", lambda *a: None)
    return web, page, calls


def changed(**parts):
    return dict(CTX, **parts)


@pytest.mark.parametrize("after,moved", [
    (CTX, []),
    (changed(forms=["CustTable", "SysBoxForm"], dialog="dialog:SysBoxForm"), ["form", "dialog"]),
    (changed(record={"caption": "US-002", "rows": {"Grid": "76"}}), ["record"]),
    (changed(pane={"action_tab": "aptabSell", "tabs": [], "menu": False}), ["pane"]),
    (changed(fields="abd"), ["fields"]),
])
def test_context_moves_names_each_part_that_moved(after, moved):
    assert context_moves(CTX, after) == moved


@pytest.mark.parametrize("before,after", [({"error": "x"}, CTX), (CTX, None), ([], CTX)])
def test_an_unreadable_context_says_nothing(before, after):
    assert context_moves(before, after) is None


def test_a_press_that_moved_nothing_says_unchanged_and_presses_once(monkeypatch):
    web, page, calls = stub(monkeypatch, [CTX], model={"acted": "Clicked"})
    result = web._op_press("Btn", 0)
    assert result["unchanged"] is True and result["note"] == UNCHANGED_NOTE and "changed" not in result
    assert calls.count("model:press") == 1 and page.events == []  # no retry, no click after the model


def test_a_press_that_opened_a_dialog_says_what_moved(monkeypatch):
    web, page, calls = stub(monkeypatch, [CTX, changed(forms=["CustTable", "Dlg"], dialog="dialog:Dlg")],
                            model={"acted": "Clicked"})
    result = web._op_press("Btn", 0)
    assert result["changed"] == ["form", "dialog"] and "unchanged" not in result


@pytest.mark.parametrize("state,moved", [
    ({"new_messages": [{"severity": "info", "text": "Done"}]}, ["messages"]),
    ({"browser_dialogs_accepted": ["alert: hi"]}, ["browser_dialog"]),
])
def test_messages_and_browser_dialogs_count_as_moves(monkeypatch, state, moved):
    web, page, calls = stub(monkeypatch, [CTX], model={"acted": "Clicked"}, state=state)
    assert web._op_press("Btn", 0)["changed"] == moved


def test_a_new_address_counts_as_a_move(monkeypatch):
    web, page, calls = stub(monkeypatch, [CTX], model=None)
    real = page.click
    page.click = lambda x, y: (real(x, y), setattr(page, "address", "/?mi=SalesTable"))
    assert web._op_press("Btn", 0)["changed"] == ["address"]


def test_nothing_is_claimed_while_d365_is_still_busy_or_the_page_unreadable(monkeypatch):
    web, page, calls = stub(monkeypatch, [CTX], model={"acted": "Clicked"}, state={"idle": False})
    result = web._op_press("Btn", 0)
    assert "unchanged" not in result and "changed" not in result
    web, page, calls = stub(monkeypatch, [None], model={"acted": "Clicked"})
    result = web._op_press("Btn", 0)
    assert "unchanged" not in result and "changed" not in result


def test_select_row_reports_the_active_record(monkeypatch):
    after = changed(record={"caption": "US-002", "rows": {"Grid": "76"}})
    active = {"grid": "Grid", "row": 1, "key": "3", "values": ["US-002", "Contoso"], "tracking": "76"}
    web, page, calls = stub(monkeypatch, [CTX, after], active=active, state={"caption": "US-002"})
    result = web._op_select_row("Grid", 1, None)
    assert result["selected"] == {"grid": "Grid", "row": 1}
    assert result["active"] == {"grid": "Grid", "row": 1, "key": "3", "values": ["US-002", "Contoso"],
                                "caption": "US-002"}
    assert result["changed"] == ["record"] and "active_differs" not in result


def test_select_row_says_when_the_active_record_is_not_the_clicked_row(monkeypatch):
    web, page, calls = stub(monkeypatch, [CTX], active={"grid": "Grid", "row": 0, "key": "2", "values": ["US-001"]})
    result = web._op_select_row("Grid", 1, None)
    assert result["active_differs"] is True and result["unchanged"] is True
    assert result["note"] == UNCHANGED_NOTE


def test_select_row_on_the_already_active_row_says_so(monkeypatch):
    web, page, calls = stub(monkeypatch, [CTX], active={"grid": "Grid", "row": 1, "key": "3", "values": ["US-001"]})
    result = web._op_select_row("Grid", 1, None)
    assert result["unchanged"] is True and "already the active record" in result["note"]


def test_select_row_without_a_grid_viewmodel_reports_only_the_click(monkeypatch):
    web, page, calls = stub(monkeypatch, [CTX, changed(fields="x")], active={"grid": "Grid", "unknown": True})
    result = web._op_select_row("Grid", 1, None)
    assert "active" not in result and result["changed"] == ["fields"]


def test_the_scenario_transcript_carries_unchanged(monkeypatch):
    web, page, calls = stub(monkeypatch, [CTX], model={"acted": "Clicked"},
                            state={"scope": "form", "form": "CustTable"})
    result = web._op_scenario([{"op": "press", "name": "Btn"}], None, True)
    assert result["steps"][0]["unchanged"] is True and result["steps"][0]["pressed"] == "Btn"
