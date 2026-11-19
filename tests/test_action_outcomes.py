"""Raising-site decisions against synthetic pages, through the real MCP adapters."""
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from boring_pondnt import d365web_client as module
from boring_pondnt.d365web_client import Config, WebClient, D365WebError
from boring_pondnt.d365web_browser import BrowserError, Page as BrowserPage
from boring_pondnt.d365web_server import create_server
from boring_pondnt.d365whsapp_client import WhsClient, WhsError, WorkerSession
from boring_pondnt.d365whsapp_backends import FormBackend
from boring_pondnt.d365whsapp_server import _outcome as whs_outcome
from boring_pondnt.d365whsapp_protocol import parse_page, build_request, ProtocolError
from boring_pondnt.d365_entra import Response, read_dpapi_secret
from boring_pondnt.whs_logoff import LookupPending
from test_d365web_server import call
from test_d365whsapp_client import FakeAos, client as app_client, sample


class Page:
    def __init__(self):
        self.events = []

    def click(self, *args): self.events.append(("click", *args))
    def key(self, *args): self.events.append(("key", *args))
    def type_text(self, *args): self.events.append(("type", *args))
    def navigate(self, *args): self.events.append(("navigate", *args))
    def send(self, *args): self.events.append(("send", *args))
    def move(self, *args): self.events.append(("move", *args))
    def url(self): return "https://fixture.invalid/?cmp=usmf"


@pytest.fixture
def rig(monkeypatch, tmp_path):
    # No browser, worker thread, D365 host or network. The operation and adapters
    # are the real implementation; only their external page is a stand-in.
    web = object.__new__(WebClient)
    web.config = Config(url="https://fixture.invalid", backend="input")
    web.config.profile_dir = lambda: tmp_path / "profile"
    web.budget, web.state_dir, web._acted = 30, None, False
    web.browser, web.dismissed, web.last_used = None, [], 0
    web.page = page = Page()
    web._ensure = lambda: page
    web._wait_idle = lambda *a: True
    web._after_step = lambda before, **kwargs: kwargs
    web._context = lambda: {}
    web._report_moves = lambda *a: None
    web._js = lambda op, *a: [] if op == "messages" else {}
    web.call = lambda label, fn, *a, **kw: web._guarded(fn)
    monkeypatch.setattr(module.time, "sleep", lambda *a: None)
    monkeypatch.setattr(module, "READONLY_GRACE", 0)
    return web, page


def answer(web, tmp_path, operation, prefix):
    def work():
        if prefix:
            web._op_key("Tab")  # real earlier input in this same admitted operation
        return operation()
    web.press = lambda *a, **kw: web.call("fixture", work)
    return call(create_server(web, tmp_path), "web_press", {"name": "fixture"})


# Each row drives the named real raising site. Branches sharing a line are
# separate cases when the absence/presence of an earlier action differs.
WEB_CASES = [
    ("parse_field_key", "invalid_request", False),
    ("as_bool", "invalid_request", False),
    ("locate_tab", "ambiguous", False),
    ("locate_missing", "not_found", False),
    ("upload", "unsupported", False),
    ("uncover_gone", "not_found", True),
    ("uncover_still_covered", "covered", True),
    ("covered_without_dismissal", "covered", False),
    ("open_bad_menu", "invalid_request", False),
    ("open_bad_company", "invalid_request", False),
    ("open_unknown", "unavailable", False),
    ("open_unsaved", "conflict", False),
    ("reference_group", "unsupported", False),
    ("readonly", "conflict", False),
    ("model_no_option", "not_found", False),
    ("one_line", "invalid_request", False),
    ("focus", "conflict", True),
    ("combo_covered", "covered", True),
    ("combo_missing", "not_found", True),
    ("disabled", "conflict", False),
    ("lookup_model_missing", "not_found", True),
    ("lookup_typed_missing", "not_found", True),
    ("lookup_ambiguous", "ambiguous", True),
    ("lookup_did_not_open", "no_lookup", True),
    ("dispatch_unknown", "invalid_request", False),
    ("filter_model_column", "not_found", False),
    ("filter_missing", "not_found", False),
    ("filter_typed_column", "not_found", True),
    ("scan_grid_missing", "not_found", False),
    ("row_ambiguous", "ambiguous", False),
    ("row_links_only", "unsupported", False),
    ("row_missing", "not_found", False),
    ("tree_no_expand", "unsupported", False),
    ("tree_no_answer", "not_found", False),
    ("tree_ambiguous", "ambiguous", False),
    ("tree_missing", "not_found", False),
    ("node_ambiguous", "ambiguous", False),
    ("node_missing", "not_found", False),
    ("grid_missing", "not_found", False),
    ("foreign_browser", "browser", False),
    ("signin", "signin_required", False),
]


def web_case(web, page, monkeypatch, case):
    found = {"x": 1, "y": 2, "control": "X", "role": "Input", "value": ""}
    web._locate = lambda *a: dict(found)
    def route(values):
        web._js = lambda op, *a: values.get(op, [] if op == "messages" else {})
    if case == "parse_field_key":
        return lambda: web._op_set([("", "value")])
    if case == "as_bool":
        return lambda: web._input_set(page, {**found, "check": True, "value": "No"},
                                       "X", (0, False), "not-a-bool", [])
    if case.startswith("locate_"):
        route({"locate": {"error": "not_found", **(
            {"reason": "tab_header_unavailable"} if case == "locate_tab" else {})}})
        return lambda: WebClient._locate(web, "X", 0, "field")
    if case == "upload":
        return lambda: web._refuse_upload({"upload": "Upload"}, "X")
    if case.startswith("uncover_") or case == "covered_without_dismissal":
        route({"tipButton": {"x": 3, "y": 4},
               "locate": {"error": "not_found"} if case == "uncover_gone" else {"covered": {"by": "shield"}}})
        cover = {"by": "shield"} if case == "covered_without_dismissal" else {"tip": "tip"}
        return lambda: web._uncover("X", 0, "field", {"covered": cover})
    if case.startswith("open_"):
        route({"model:dirty": None if case == "open_unknown" else {"unsaved": ["Form"]}})
        return lambda: web._op_open("bad!" if case == "open_bad_menu" else "Menu",
                                   "bad!" if case == "open_bad_company" else "usmf", None)
    if case in {"reference_group", "readonly"}:
        found.update({"role": "ReferenceGroup", "parts": ["X"]} if case == "reference_group" else {"readonly": True})
        return lambda: web._writable("X", 0, False, [])
    if case == "model_no_option":
        web.config.backend = "model"
        route({"model:set": {"error": "no_option", "options": ["a", "b"]}})
        return lambda: web._model_set({**found, "role": "ComboBox"}, "X", "missing", [])
    if case in {"one_line", "focus"}:
        route({"focusedAt": False})
        return lambda: web._input_set(page, found, "X", (0, False),
                                      "a\nb" if case == "one_line" else "value", [])
    if case.startswith("combo_"):
        route({"listOpen": False, "comboOption": {"error": "covered" if case == "combo_covered" else "not_found",
                                                 "options": []}})
        return lambda: web._pick_combo(page, found, "X", "value", [])
    if case == "disabled":
        found["enabled"] = False
        return lambda: web._op_press("X", 0)
    if case == "lookup_model_missing":
        web.config.backend = "model"
        state = {"scope": "form"}
        def js(op, *args):
            if op == "scope": return {"scope": state["scope"]}
            if op == "model:openLookup":
                state["scope"] = "lookup"
                return {"acted": "openLookup"}
            if op == "model:lookupPick": return {"fallback": "missing", "complete": True}
            if op == "model:lookupCancel": return {"acted": "RequestCloseCancel"}
            if op == "row": return {"error": "not_found"}
            return [] if op == "messages" else {}
        web._js = js
        return lambda: web._op_lookup("X", 0, "missing")
    if case == "lookup_typed_missing":
        route({"scope": {"scope": "lookup"}, "row": {"error": "not_found"},
               "listOpen": True, "locate": {"value": "filter text"}})
        web._wait_list = lambda *a: True
        return lambda: web._op_lookup("X", 0, "missing")
    if case == "lookup_ambiguous":
        route({"model:lookupCancel": {"acted": "RequestCloseCancel"}})
        return lambda: web._refuse_ambiguous_pick(page, "X", {"matches": 2})
    if case == "lookup_did_not_open":
        route({"scope": {"scope": "form"}, "listOpen": False})
        return lambda: web._open_lookup(page, "X", 0, None)
    if case == "dispatch_unknown":
        return lambda: web._dispatch("unknown", {})
    if case.startswith("filter_"):
        if case == "filter_model_column":
            web.config.backend = "model"
            route({"model:filter": {"error": "no_column", "columns": []}})
        else:
            route({"quickfilter": {"error": "not_found"} if case == "filter_missing" else {"x": 1, "y": 2},
                   "filteroption": {"error": "not_found", "columns": []}})
            # No time loop in the fixture; one observed answer ends it.
            web._js = (lambda js: lambda op, *a: {"error": "not_found", "columns": ["other"]}
                       if op == "filteroption" else js(op, *a))(web._js)
        return lambda: web._op_filter("typed", "missing")
    if case in {"scan_grid_missing", "grid_missing"}:
        route({"grid": {"error": "not_found"}})
        return (lambda: web._scan_grid("G")) if case == "scan_grid_missing" else (lambda: web._op_grid("G", 5))
    if case.startswith("row_"):
        route({"row": {"error": {"row_ambiguous": "ambiguous", "row_links_only": "links_only",
                                 "row_missing": "not_found"}[case], "grid": "G", "row": 0, "matches": 2}})
        return lambda: web._op_select_row("G", 0, None)
    if case == "tree_no_expand":
        web._tree_node = lambda *a: {"tree": "T", "id": "1", "expanded": False}
        return lambda: web._op_tree("T", ["parent", "child"])
    if case.startswith("tree_") or case.startswith("node_"):
        node = None if case == "tree_no_answer" else {
            "error": "ambiguous" if case.endswith("ambiguous") else "not_found",
            "what": "tree" if case.startswith("tree_") else "node"}
        route({"treeNode": node})
        return lambda: web._tree_node("T", None, "X", [])
    if case == "signin":
        web.page = None  # no browser dialog to acknowledge before inspecting status
        route({"status": "signin"})
        return lambda: WebClient._wait_idle(web)
    if case == "foreign_browser":
        web.config.debug_port = 9333
        def foreign(*args): raise BrowserError("foreign", "another profile")
        monkeypatch.setattr(module.Browser, "attach", foreign)
        return web._connect
    raise AssertionError(case)


@pytest.mark.parametrize("prefix", [False, True], ids=["untouched", "after-input"])
@pytest.mark.parametrize("case,code,already_acts", WEB_CASES, ids=[x[0] for x in WEB_CASES])
def test_web_raising_site_matches_actions(rig, monkeypatch, tmp_path, prefix, case, code, already_acts):
    web, page = rig
    operation = web_case(web, page, monkeypatch, case)
    error, data = answer(web, tmp_path, operation, prefix)
    assert error and data["code"] == code
    assert data["status"] == ("failed" if prefix or already_acts else "refused")
    if data["status"] == "failed":
        assert data["effects"] == "unknown"
        assert page.events or case in {"lookup_model_missing", "lookup_ambiguous"}
    else:
        assert page.events == [] and "effects" not in data


@pytest.mark.parametrize("prefix", [False, True])
@pytest.mark.parametrize("key", ["§", "Bogus+X"])
def test_locally_invalid_keys_do_not_count_as_delivered_input(rig, tmp_path, prefix, key):
    web, page = rig
    page.key = lambda combo: BrowserPage.key(page, combo)
    error, data = answer(web, tmp_path, lambda: web._op_key(key), prefix)
    assert error and data["code"] == "browser" and data["browser_code"] == "cdp_error"
    assert data["status"] == ("failed" if prefix else "refused")
    assert len(page.events) == (2 if prefix else 0)
    assert ("effects" in data) is prefix


@pytest.mark.parametrize("result", [{"fallback": "no model"}, {"acted": "none"},
                                   {"error": "no_option"}])
def test_model_preflight_answers_are_not_actions(rig, tmp_path, result):
    web, page = rig
    web._js = lambda *a: result
    web._locate = lambda *a: {"enabled": False}
    def work():
        web._model_js("model:set")
        return web._op_press("disabled", 0)
    error, data = answer(web, tmp_path, work, False)
    assert error and data["status"] == "refused" and data["code"] == "conflict"
    assert page.events == []


def test_action_history_resets_at_the_next_operation(rig, tmp_path):
    web, page = rig
    web._locate = lambda *a: {"enabled": False}
    error, first = answer(web, tmp_path, lambda: web._op_press("X", 0), True)
    assert error and first["status"] == "failed" and first["code"] == "conflict"
    page.events.clear()
    error, second = answer(web, tmp_path, lambda: web._op_press("X", 0), False)
    assert error and second["status"] == "refused" and second["code"] == "conflict"
    assert page.events == []


@pytest.mark.parametrize("action", ["model", "scroll", "tree"])
def test_non_keyboard_actions_carry_uncertainty_to_a_later_guard(rig, tmp_path, action):
    web, page = rig
    def work():
        if action == "model":
            web._js = lambda op, *a: {"acted": "SetValue"} if op == "model:set" else {}
            web._model_set({"x": 1, "y": 2, "control": "X", "role": "Input"}, "X", "value", [])
        elif action == "scroll":
            web._js = lambda *a: {"before": 0, "after": 100}
            web._scroll_grid("G", "down")
        else:
            nodes = iter([{"tree": "T", "id": "1", "expanded": False, "bx": 1, "by": 2},
                          {"tree": "T", "id": "2", "expanded": False}])
            web._tree_node = lambda *a: next(nodes)
            return web._op_tree("T", ["A", "B", "C"])
        web._locate = lambda *a: {"enabled": False}
        return web._op_press("X", 0)
    web.config.backend = "model"
    error, data = answer(web, tmp_path, work, False)
    assert error and data["status"] == "failed" and data["effects"] == "unknown"


@pytest.mark.parametrize("case", ["readonly", "disabled"])
def test_whs_protocol_page_state_is_refused_conflict_before_submit(tmp_path, case):
    from boring_pondnt.d365whsapp_server import create_server as whs_server
    from test_d365whsapp_server import call as whs_call
    fake = FakeAos()
    app = app_client(fake)
    page = parse_page(sample("poreceive-qty-tab.xml"))
    if case == "disabled":
        page.controls = [replace(c, enabled=False) if c.name == "OK" else c for c in page.controls]
    app.sessions["W1"] = WorkerSession("W1", "usmf", "device", page=page)
    args = {"worker": "W1", **({"values": {"PONum": "x"}} if case == "readonly" else {"press": "OK"})}
    error, data = whs_call(whs_server(app, tmp_path), "whs_submit", args)
    assert error and data["status"] == "refused" and data["code"] == "conflict"
    assert not fake.requests and not fake.token_calls


def test_whs_login_validation_follows_a_dispatched_page_request(tmp_path):
    from boring_pondnt.d365whsapp_server import create_server as whs_server
    from test_d365whsapp_server import call as whs_call
    fake = FakeAos("login.xml")
    error, data = whs_call(whs_server(app_client(fake), tmp_path), "whs_login",
                           {"worker": "W" * 11, "password": "pw"})
    assert error and data["status"] == "failed" and data["code"] == "limit"
    assert data["effects"] == "unknown" and len(fake.requests) == 1
    assert fake.requests[0]["_xml"] == ""


@pytest.mark.parametrize("sent", [False, True])
def test_whs_token_errors_keep_dispatch_evidence(tmp_path, monkeypatch, sent):
    fake = FakeAos()
    app = app_client(fake)
    if sent:
        app.tokens.transport = lambda *a, **kw: Response(401, '{"error":"invalid_client"}')
    else:
        monkeypatch.setattr("boring_pondnt.d365_entra.sys.platform", "linux")
        app.tokens.secret = lambda: read_dpapi_secret("missing")
    with pytest.raises(WhsError) as caught:
        app.call("getCompanies", {})
    data = whs_outcome(caught.value).as_data()
    assert data["status"] == ("failed" if sent else "refused")
    assert ("effects" in data) is sent
    assert not fake.requests
