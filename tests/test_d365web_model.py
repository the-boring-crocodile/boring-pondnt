"""The model backend: one viewmodel action per located control; the input path only when it did not act.

These tests drive WebClient._op_set/_op_press with a fake page route (_js) and a page that records
input events, so they show which backend acted and that no input event follows a model action.
Live behaviour against D365 is checked on a dev box."""
import time

import pytest

from boring_pondnt import d365web_model
from boring_pondnt.d365web_client import Config, D365WebError, WebClient


class Page:
    def __init__(self):
        self.events = []

    def click(self, x, y):
        self.events.append(("click", x, y))

    def key(self, combo):
        self.events.append(("key", combo))

    def type_text(self, text):
        self.events.append(("type", text))


def client(monkeypatch, model_answer, role="Input", value="", check=False, backend="model"):
    web = WebClient(Config(url="https://example.test", backend=backend))
    page = Page()
    calls = []
    found = {"control": "Field1", "x": 10, "y": 20, "role": role, "value": value, "check": check}

    def js(op, *args):
        calls.append((op, args))
        if op.startswith("model:"):
            return model_answer
        if op == "locate":
            return {"value": "after"}
        return []

    monkeypatch.setattr(web, "_ensure", lambda: page)
    monkeypatch.setattr(web, "_locate", lambda *a: dict(found))
    monkeypatch.setattr(web, "_wait_idle", lambda: True)
    monkeypatch.setattr(web, "_js", js)
    monkeypatch.setattr(web, "_after_step", lambda before, **kw: kw)
    monkeypatch.setattr(time, "sleep", lambda *a: None)
    return web, page, calls


def test_a_model_action_sends_no_input_events(monkeypatch):
    web, page, calls = client(monkeypatch, {"acted": "SetValue", "type": "Input"})
    result = web._op_set([("Field1", "after")])
    assert page.events == []
    assert result["applied"] == [{"field": "Field1", "value": "after", "via": "model", "now": "after"}]
    assert ("model:set", (10, 20, "Field1", "after", None)) in calls


@pytest.mark.parametrize("answer", [{"fallback": "no viewmodel at the control"}, None, {}, []])
def test_no_model_action_uses_the_input_path_and_says_so(monkeypatch, answer):
    web, page, _ = client(monkeypatch, answer)
    result = web._op_set([("Field1", "after")])
    assert ("type", "after") in page.events
    assert result["applied"][0]["via"] == "input"


def test_a_missing_combo_option_is_refused_with_the_options_and_no_input(monkeypatch):
    web, page, _ = client(monkeypatch, {"error": "no_option", "type": "ComboBox", "options": ["Pick", "Put"]},
                          role="ComboBox")
    with pytest.raises(D365WebError) as caught:
        web._op_set([("Field1", "Nothing")])
    assert caught.value.details["options"] == ["Pick", "Put"]
    assert page.events == []


def test_a_checkbox_passes_the_wanted_state(monkeypatch):
    web, page, calls = client(monkeypatch, {"acted": "toggleValue", "type": "CheckBox"}, check=True, value="No")
    web._op_set([("Field1", "yes")])
    assert ("model:set", (10, 20, "Field1", "yes", True)) in calls
    assert page.events == []


def test_press_through_the_model_does_not_click(monkeypatch):
    web, page, calls = client(monkeypatch, {"acted": "Clicked", "type": "CommandButton"})
    monkeypatch.setattr(web, "_locate", lambda *a: {"control": "Btn", "x": 1, "y": 2})
    result = web._op_press("Btn", 0)
    assert page.events == [] and result["via"] == "model"


def test_press_falls_back_to_a_click(monkeypatch):
    web, page, _ = client(monkeypatch, {"fallback": "no model command for X"})
    monkeypatch.setattr(web, "_locate", lambda *a: {"control": "Btn", "x": 1, "y": 2})
    result = web._op_press("Btn", 0)
    assert page.events == [("click", 1, 2)] and result["via"] == "input"


def test_the_input_backend_never_asks_the_model(monkeypatch):
    web, page, calls = client(monkeypatch, {"acted": "SetValue"}, backend="input")
    result = web._op_set([("Field1", "after")])
    assert not any(op.startswith("model:") for op, _ in calls)
    assert "via" not in result["applied"][0] and ("type", "after") in page.events


def test_config_rejects_an_unknown_backend():
    with pytest.raises(ValueError):
        Config(url="https://example.test", backend="magic").validate()
    assert d365web_model.BACKENDS == ("model", "input")


class FakeGrid:
    """A grid of `total` rows whose server sends blocks of `block`; the DOM shows `view` rows at the scroll offset."""

    def __init__(self, total=12, block=5, view=3, model=True):
        self.total, self.block, self.view, self.model = total, block, view, model
        self.cached, self.top = block, 0

    def js(self, op, *args):
        if op == "grid":
            rows = [{"key": str(i + 2), "values": [f"L{i}"]} for i in range(self.top, min(self.top + self.view, self.cached))]
            return {"name": "Grid", "columns": [{"name": "Loc", "label": "Location"}], "rows": rows, "rowcount_hint": self.total + 1}
        if op == "gridScroll":
            before = self.top
            self.top = 0 if args[1] == "top" else min(self.top + self.view, max(0, self.cached - self.view))
            return {"before": before, "after": self.top, "scrollable": True}
        if op == "model:loadPage":
            if not self.model:
                return {"fallback": "grid without LoadNextPage"}
            if self.cached >= self.total:
                return {"acted": "none", "has_next": False, "rows": self.cached}
            self.cached = min(self.total, self.cached + self.block)
            return {"acted": "LoadNextPage", "has_next": True, "rows": self.cached}
        if op == "model:hasNextPage":
            return {"has_next": self.cached < self.total, "rows": self.cached}
        if op == "scope":
            return {"scope": "form", "form": "WMSLocation"}
        raise AssertionError(f"unexpected op {op}")


def scanner(monkeypatch, grid, backend="model"):
    web = WebClient(Config(url="https://example.test", backend=backend))
    page = Page()
    monkeypatch.setattr(web, "_ensure", lambda: page)
    monkeypatch.setattr(web, "_wait_idle", lambda: True)
    monkeypatch.setattr(web, "_js", grid.js)
    monkeypatch.setattr(time, "sleep", lambda *a: None)
    return web, page


def test_model_grid_scan_loads_all_blocks_without_input(monkeypatch):
    grid = FakeGrid()
    web, page = scanner(monkeypatch, grid)
    result = web._scan_grid("Grid")
    assert [r["values"][0] for r in result["rows"]] == [f"L{i}" for i in range(12)]
    assert result["complete"] is True and "selection_moved" not in result  # scrolling moves no selection
    assert page.events == []


def test_model_grid_scan_without_a_viewmodel_stays_incomplete_and_sends_no_input(monkeypatch):
    grid = FakeGrid(model=False)
    web, page = scanner(monkeypatch, grid)
    result = web._scan_grid("Grid")
    assert len(result["rows"]) == 5
    assert result["complete"] is False
    assert page.events == []


def test_load_all_pages_stops_at_the_cap_and_the_deadline():
    grid = FakeGrid(total=100, block=10)
    assert d365web_model.load_all_pages(grid.js, "Grid", lambda: None, 30, lambda: True) == {"pages": 2, "complete": False, "rows": 30}
    grid = FakeGrid(total=100, block=10)
    assert d365web_model.load_all_pages(grid.js, "Grid", lambda: None, 1000, lambda: False) == {"pages": 0, "complete": False}
    assert d365web_model.load_all_pages(FakeGrid(model=False).js, "Grid", lambda: None, 1000, lambda: True) is None


def test_model_quick_filter_sends_no_input(monkeypatch):
    web, page, calls = client(monkeypatch, {"acted": "ApplyFilter", "column": "Location", "field": "WMSLocation_WMSLocationId"})
    monkeypatch.setattr(web, "_after_step", lambda before, **kw: kw)
    result = web._op_filter("01", "Location")
    assert page.events == []
    assert result == {"filter": {"column": "Location", "value": "01"}, "via": "model"}
    assert ("model:filter", ("01", "Location", "")) in calls


def test_model_quick_filter_unknown_column_is_refused_with_the_columns(monkeypatch):
    web, page, calls = client(monkeypatch, {"error": "no_column", "columns": ["Warehouse", "Location"]})
    with pytest.raises(D365WebError) as raised:
        web._op_filter("01", "Nope")
    assert raised.value.code == "not_found" and raised.value.details["columns"] == ["Warehouse", "Location"]
    assert page.events == []


def test_model_quick_filter_clear(monkeypatch):
    web, page, calls = client(monkeypatch, {"acted": "callClearFilter", "cleared": True})
    result = web._op_filter("", None)
    assert result["filter"] == {"cleared": True} and page.events == []


def lookup_client(monkeypatch, pick_answers, row_missing=False):
    web, page, calls = client(monkeypatch, None)
    state = {"scope": "form"}
    answers = list(pick_answers)

    def js(op, *args):
        calls.append((op, args))
        if op == "scope":
            return {"scope": state["scope"]}
        if op == "model:openLookup":
            state["scope"] = "lookup"
            return {"acted": "openLookup"}
        if op == "model:lookupPick":
            return answers.pop(0)
        if op == "model:lookupCommit":
            state["scope"] = "form"
            return {"acted": "SelectionCompleted"}
        if op == "model:lookupCancel":
            state["scope"] = "form"
            return {"acted": "RequestCloseCancel"}
        if op == "row":
            return {"error": "not_found"} if row_missing else {"x": 5, "y": 6}
        if op == "grid":
            return {"rows": []}
        return []

    monkeypatch.setattr(web, "_js", js)
    return web, page, calls


def test_model_lookup_opens_picks_and_commits_without_input(monkeypatch):
    web, page, calls = lookup_client(monkeypatch, [{"loading": True}, {"acted": "changeRow", "index": 4}])
    result = web._op_lookup("WhenItem_ItemId", 0, "A0001")
    assert page.events == []
    assert result == {"picked": "A0001", "via": "model"}
    assert [op for op, _ in calls if op.startswith("model:")] == [
        "model:openLookup", "model:lookupPick", "model:lookupPick", "model:lookupCommit"]


def test_model_lookup_without_the_value_in_its_data_uses_the_input_path(monkeypatch):
    web, page, calls = lookup_client(monkeypatch, [{"fallback": "no row with that value in the lookup data"}])
    result = web._op_lookup("WhenItem_ItemId", 0, "Bulk location")
    assert page.events == [("click", 5, 6)]  # the input path clicks the rendered row with that text
    assert result == {"picked": "Bulk location"}


def test_model_lookup_open_only(monkeypatch):
    web, page, calls = lookup_client(monkeypatch, [])
    assert web._op_lookup("WhenItem_ItemId", 0, None) == {"via": "model"} and page.events == []


def test_model_lookup_miss_over_all_rows_is_refused_without_typing(monkeypatch):
    web, page, calls = lookup_client(monkeypatch, [{"fallback": "no row", "complete": True}], row_missing=True)
    with pytest.raises(D365WebError) as raised:
        web._op_lookup("WhenItem_ItemId", 0, "ZZNOSUCHITEM")
    assert raised.value.code == "not_found"
    assert page.events == []  # nothing typed into the field, no Escape
    assert ("model:lookupCancel", ()) in calls


def test_set_stops_when_a_field_opens_a_dialog(monkeypatch):
    web, page, calls = client(monkeypatch, {"acted": "SetValue", "type": "Input"})
    scopes = [{"scope": "form", "form": "CustTable"}, {"scope": "dialog", "form": "SysBoxForm"}]
    inner = web._js
    def js(op, *args):
        if op == "scope":
            return scopes.pop(0) if len(scopes) > 1 else scopes[0]
        return inner(op, *args)
    monkeypatch.setattr(web, "_js", js)
    result = web._op_set([("Posting_CustGroup", "10"), ("OrgDetailGroup_ABC", "B")])
    assert result["stopped_at"] == "Posting_CustGroup"
    assert [line["field"] for line in result["applied"]] == ["Posting_CustGroup"]


def test_unsaved_forms_are_read_safely():
    assert d365web_model.unsaved_forms(lambda op: {"unsaved": ["CustTable"]}) == ["CustTable"]
    assert d365web_model.unsaved_forms(lambda op: []) == []
    def broken(op):
        raise RuntimeError("page gone")
    assert d365web_model.unsaved_forms(broken) == []


def test_open_refuses_while_d365_reports_unsaved_changes(monkeypatch):
    web, page, calls = client(monkeypatch, {"unsaved": ["SalesTable"]})
    page.navigate = lambda url: page.events.append(("navigate", url))
    with pytest.raises(D365WebError) as raised:
        web._op_open("CustTable", "usmf", None)
    assert raised.value.code == "refused" and raised.value.details["unsaved"] == ["SalesTable"]
    assert page.events == []


def test_open_navigates_when_nothing_is_unsaved(monkeypatch):
    web, page, calls = client(monkeypatch, {"unsaved": []})
    page.navigate = lambda url: page.events.append(("navigate", url))
    web._op_open("CustTable", "usmf", None)
    assert page.events == [("navigate", "https://example.test/?cmp=usmf&mi=CustTable")]


def test_an_iso_date_through_the_model_is_compared_by_its_raw_value(monkeypatch):
    web, page, calls = client(monkeypatch, {"acted": "SetValue", "type": "Date", "raw": True}, role="Date")
    inner = web._js
    def js(op, *args):
        if op == "locate":
            return {"value": "11/20/2026"}
        if op == "model:rawValue":
            return {"value": "2026-11-20"}
        return inner(op, *args)
    monkeypatch.setattr(web, "_js", js)
    line = web._op_set([("Delivery_ShippingDateRequestedHeader", "2026-11-20")])["applied"][0]
    assert line["via"] == "model" and line["now"] == "11/20/2026" and "differs" not in line
    assert page.events == []


@pytest.mark.parametrize("empty,flag", [(True, "differs"), (False, "confirmed")])
def test_a_password_through_the_model_is_confirmed_by_d365s_own_state(monkeypatch, empty, flag):
    web, page, calls = client(monkeypatch, {"acted": "SetValue", "type": "Input"})
    inner = web._js
    def js(op, *args):
        if op == "model:secretState":
            return {"empty": empty}
        if op == "locate":
            return {"value": ""}  # a password input reads back empty
        return inner(op, *args)
    monkeypatch.setattr(web, "_js", js)
    line = web._op_set([("Password", "1")])["applied"][0]
    assert line["value"] != "1" and line.get(flag) is True
    assert page.events == []


# --- review fixes (rc7) -----------------------------------------------------------------------------

def test_an_unexpected_model_error_means_nothing_was_sent_and_input_acts(monkeypatch):
    web, page, _ = client(monkeypatch, {"error": "unknown op"})
    line = web._op_set([("Field1", "after")])["applied"][0]
    assert ("type", "after") in page.events and line["via"] == "input"


@pytest.mark.parametrize("active,via", [(False, "input"), (True, "model")])
def test_a_grid_row_is_acted_on_through_the_model_only_when_it_is_the_active_row(monkeypatch, active, via):
    # D365 applies a viewmodel command to the grid's current record: another row is clicked first.
    web, page, calls = client(monkeypatch, {"acted": "SetValue", "type": "Input"})
    monkeypatch.setattr(web, "_locate", lambda *a: {"control": "Field1", "x": 10, "y": 20, "role": "Input",
                                                    "value": "", "in_row": True, "active_row": active})
    line = web._op_set([("Field1", "after")])["applied"][0]
    assert line["via"] == via
    assert any(op == "model:set" for op, _ in calls) is active
    assert (("type", "after") in page.events) is not active


def test_a_grid_row_button_other_than_the_active_row_is_clicked(monkeypatch):
    web, page, calls = client(monkeypatch, {"acted": "Clicked", "type": "CommandButton"})
    monkeypatch.setattr(web, "_locate", lambda *a: {"control": "Btn", "x": 1, "y": 2, "in_row": True, "active_row": False})
    assert web._op_press("Btn", 1)["via"] == "input" and page.events == [("click", 1, 2)]
    assert not any(op == "model:press" for op, _ in calls)


def test_a_combo_already_holding_the_value_sends_nothing(monkeypatch):
    web, page, calls = client(monkeypatch, {"acted": "SetSelectedItem"}, role="ComboBox", value="Pick")
    line = web._op_set([("Field1", "pick")])["applied"][0]
    assert line["via"] == "model" and page.events == []
    assert not any(op == "model:set" for op, _ in calls)


@pytest.mark.parametrize("key,explicit", [("Field1", False), ("Field1[0]", True), ("Field1[2]", True)])
def test_an_explicit_occurrence_is_passed_on_so_0_means_the_first_row(monkeypatch, key, explicit):
    web, page, calls = client(monkeypatch, {"acted": "SetValue", "type": "Input"})
    seen = []
    monkeypatch.setattr(web, "_locate", lambda name, nth, kind, ex=False: seen.append((nth, ex)) or
                        {"control": "Field1", "x": 10, "y": 20, "role": "Input", "value": ""})
    web._op_set([(key, "after")])
    assert seen[0][1] is explicit
    assert any(op == "locate" and args[3] is explicit for op, args in calls)


def test_raw_value_and_secret_state_are_read_at_the_written_control(monkeypatch):
    web, page, calls = client(monkeypatch, {"acted": "SetValue", "type": "Date", "raw": True}, role="Date")
    inner = web._js
    def js(op, *args):
        if op == "locate":
            return {"value": "11/20/2026", "x": 30, "y": 40}
        if op == "model:rawValue":
            calls.append((op, args))
            return {"value": "2026-11-20T00:00:00"}
        return inner(op, *args)
    monkeypatch.setattr(web, "_js", js)
    line = web._op_set([("Field1", "2026-11-20")])["applied"][0]
    assert "differs" not in line
    assert ("model:rawValue", (30, 40, "Field1")) in calls


@pytest.mark.parametrize("want,raw,ok", [
    ("2026-11-20", "2026-11-20", True), ("2026-11-20", "2026-11-20T00:00:00", True),
    ("2026-11-20", "2026-11-20T08:00:00", False), ("2026-11-2", "2026-11-20", False),
    ("2026-11-20T08:30", "2026-11-20T08:30:00", True), ("2026-11-20T08:30", "2026-11-20T08:31:00", False)])
def test_iso_values_compare_exactly(want, raw, ok):
    from boring_pondnt.d365web_client import iso_matches
    assert iso_matches(want, raw) is ok


def test_the_quick_filter_is_asked_for_the_scope_form_and_other_errors_fall_back():
    calls = []
    def js(op, *args):
        calls.append((op, args))
        return {"error": "something else"}
    assert d365web_model.filter_grid(js, "01", "Location", "Dialog1") is None
    assert calls == [("model:filter", ("01", "Location", "Dialog1"))]


def test_model_filter_sends_the_dialog_form(monkeypatch):
    web, page, calls = client(monkeypatch, {"acted": "ApplyFilter", "column": "Location"})
    inner = web._js
    monkeypatch.setattr(web, "_js", lambda op, *a: {"scope": "dialog", "form": "Dlg"} if op == "scope" else inner(op, *a))
    web._op_filter("01", "Location")
    assert ("model:filter", ("01", "Location", "Dlg")) in calls


def test_grid_paging_stops_when_pages_bring_no_rows():
    asked = []
    def js(op, *args):
        asked.append(op)
        if op == "model:loadPage":
            return {"acted": "LoadNextPage", "has_next": True, "rows": 50}
        return {"has_next": True, "rows": 50}
    result = d365web_model.load_all_pages(js, "Grid", lambda: True, 1000, lambda: True)
    assert result["stalled"] is True and asked.count("model:loadPage") == d365web_model.STALL_ROUNDS + 1


def test_lookup_paging_stops_when_pages_bring_no_rows():
    asked = []
    def js(op, *args):
        asked.append(op)
        return {"loading": True, "rows": 25}
    assert d365web_model.pick_lookup_row(js, "X", lambda: True, 1000, lambda: True) == {}
    assert len(asked) == d365web_model.STALL_ROUNDS + 1


@pytest.mark.parametrize("shown,differs", [("A0001", False), ("B0002", True)])
def test_a_model_lookup_pick_is_read_back(monkeypatch, shown, differs):
    web, page, calls = lookup_client(monkeypatch, [{"acted": "changeRow", "index": 0, "values": ["A0001", "Widget"]}])
    inner = web._js
    monkeypatch.setattr(web, "_js", lambda op, *a: {"value": shown, "x": 1, "y": 1} if op == "locate" else inner(op, *a))
    result = web._op_lookup("WhenItem_ItemId", None, "Widget")
    assert result["now"] == shown and result.get("differs", False) is differs


@pytest.mark.parametrize("given,explicit", [(None, False), (0, True)])
def test_a_lookup_without_nth_lets_the_active_row_choose(monkeypatch, given, explicit):
    web, page, calls = lookup_client(monkeypatch, [])
    seen = []
    monkeypatch.setattr(web, "_locate", lambda name, nth, kind, ex=False: seen.append((nth, ex)) or
                        {"control": "F", "x": 1, "y": 1})
    web._op_lookup("F", given, None)
    assert seen == [(0, explicit)]


# --- full-module review fixes ------------------------------------------------------------------------

@pytest.mark.parametrize("typed,now,differs", [
    ("ABC", "ABC1", True), ("618160", "618160-001---", False), ("1,5", "15", True),
    ("1234.5", "1,234.50", False), ("125", "125.00", False), ("abc", "ABC", False)])
def test_value_differs_allows_only_d365s_own_formatting(typed, now, differs):
    from boring_pondnt.d365web_client import value_differs
    assert value_differs(typed, now) is differs


def test_punctuation_keys_get_their_own_virtual_keys():
    from boring_pondnt.d365web_browser import Page as BrowserPage
    sent = []
    page = object.__new__(BrowserPage)
    page.send = lambda method, params=None, **kw: sent.append(params)
    page.key("Alt+.")
    assert sent[0]["windowsVirtualKeyCode"] == 190 and sent[0]["modifiers"] == 1  # not 46 (Delete)
    with pytest.raises(Exception):
        page.key("§")


def test_the_unsaved_guard_refuses_to_open_when_it_cannot_read_the_page(monkeypatch):
    web, page, calls = client(monkeypatch, None)
    def js(op, *args):
        if op == "model:dirty":
            raise RuntimeError("CDP timeout")
        return []
    monkeypatch.setattr(web, "_js", js)
    page.navigate = lambda url: page.events.append(("navigate", url))
    with pytest.raises(D365WebError) as caught:
        web._op_open("CustTable", "usmf", None)
    assert caught.value.code == "refused" and not any(e[0] == "navigate" for e in page.events)


def test_the_idle_close_keeps_a_browser_with_unsaved_changes(monkeypatch):
    from types import SimpleNamespace
    web = WebClient(Config(url="https://example.test"))
    closed = []
    web.browser, web.page = SimpleNamespace(conn=SimpleNamespace(closed=False)), object()
    web.last_used = time.monotonic() - 3600
    monkeypatch.setattr(web, "_evaluate_lib", lambda op, *a: {"unsaved": ["SalesTable"]})
    monkeypatch.setattr(web, "_close_browser", lambda: closed.append(True))
    web._close_idle()
    assert closed == [] and time.monotonic() - web.last_used < 5
    web.last_used = time.monotonic() - 3600
    monkeypatch.setattr(web, "_evaluate_lib", lambda op, *a: {"unsaved": []})
    web._close_idle()
    assert closed == [True]
    # a page that does not answer is waited for three times, then closed; a dead browser at once
    def broken(op, *a):
        raise RuntimeError("target gone")
    monkeypatch.setattr(web, "_evaluate_lib", broken)
    for expected in ([True], [True], [True], [True, True]):
        web.last_used = time.monotonic() - 3600
        web._close_idle()
        assert closed == expected
    web.browser.conn.closed = True
    web.last_used = time.monotonic() - 3600
    web._close_idle()
    assert closed == [True, True, True]
    web.browser = None
    web.shutdown()


def test_an_ambiguous_model_lookup_pick_is_refused_without_the_input_path(monkeypatch):
    web, page, calls = lookup_client(monkeypatch, [{"fallback": "more than one", "ambiguous": 2, "rows": 9}])
    with pytest.raises(D365WebError) as caught:
        web._op_lookup("WhenItem_ItemId", None, "Bolt")
    assert caught.value.code == "refused" and caught.value.details["matches"] == 2
    assert page.events == [] and not any(op == "row" for op, _ in calls)


def test_nothing_is_typed_when_the_clicked_field_has_no_focus(monkeypatch):
    web, page, calls = client(monkeypatch, None, backend="input")
    inner = web._js
    monkeypatch.setattr(web, "_js", lambda op, *a: False if op == "focusedAt" else inner(op, *a))
    with pytest.raises(D365WebError) as caught:
        web._op_set([("Field1", "after")])
    assert caught.value.code == "refused" and page.events == [("click", 10, 20)]


def test_a_line_break_is_never_typed_into_a_one_line_field(monkeypatch):
    web, page, calls = client(monkeypatch, None, backend="input")
    with pytest.raises(D365WebError):
        web._op_set([("Field1", "a\nb")])
    assert page.events == []


@pytest.mark.parametrize("typed,now,differs", [("11/6/2026", "11/6/2026 12:00:00 am", False),
                                               ("10:00", "10:00 am", False), ("ABC", "ABC def", True)])
def test_value_differs_lets_d365_complete_a_date_or_a_time(typed, now, differs):
    from boring_pondnt.d365web_client import value_differs
    assert value_differs(typed, now) is differs


def test_a_confirm_is_cancelled_and_an_alert_or_leave_question_accepted():
    import threading
    from types import SimpleNamespace
    web = WebClient(Config(url="https://example.test"))
    sent = []
    web.page = SimpleNamespace(events_lock=threading.Lock(), send=lambda m, p: sent.append(p["accept"]),
                               dialogs=[{"type": "confirm", "message": "Delete?"}, {"type": "alert", "message": "Done"},
                                        {"type": "beforeunload", "message": ""}])
    web._accept_js_dialogs()
    assert sent == [False, True, True]
    assert web.js_dialogs_dismissed == ["confirm: Delete?"] and len(web.js_dialogs) == 2
    web.page = None
    web.shutdown()


def test_the_idle_close_waits_for_logged_in_form_workers(monkeypatch):
    from types import SimpleNamespace
    web = WebClient(Config(url="https://example.test"))
    closed = []
    web.browser, web.page = SimpleNamespace(conn=SimpleNamespace(closed=False)), object()
    monkeypatch.setattr(web, "_evaluate_lib", lambda op, *a: {"unsaved": []})
    monkeypatch.setattr(web, "_close_browser", lambda: closed.append(True))
    web.keep_open = lambda: True
    web.last_used = time.monotonic() - 3600
    web._close_idle()
    assert closed == []
    web.keep_open = lambda: False
    web.last_used = time.monotonic() - 3600
    web._close_idle()
    assert closed == [True]
    web.browser = None
    web.shutdown()
