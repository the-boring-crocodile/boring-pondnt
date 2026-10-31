"""Scenario admission through real MCP entry points; effects are synthetic."""
import pytest

from boring_pondnt.d365web_client import Config, D365WebError, WebClient
from boring_pondnt.d365web_server import create_server
from boring_pondnt.d365whsapp_backends import FormBackend
from boring_pondnt.d365whsapp_server import create_server as create_whs_server
from test_d365web_server import call
from test_d365whsapp_server import server_for, FORM_S1
from test_d365whsapp_client import FakeAos
from boring_pondnt.d365web_whsform import page_from_form

WEB_BAD = [
    {"op": "open"}, {"op": []}, {"op": "press", "name": 3},
    {"op": "open", "menu_item": "X", "company": "bad&company"},
    {"op": "open", "menu_item": "action:X", "kind": "action"},
    {"op": "open", "menu_item": "../bad"}, {"op": "open", "menu_item": "X", "kind": "invalid"},
    {"op": "state", "full": "yes"}, {"op": "state", "max_rows": "many"},
    {"op": "press", "name": "OK", "nth": -1},
    {"op": "set", "values": [{"field": "X"}]},
    {"op": "set", "values": {"X": []}}, {"op": "key", "keys": ""},
    {"op": "lookup"}, {"op": "select_row", "match": ["X"]},
    {"op": "filter", "column": 4},
    {"op": "state", "expect": []},
    {"op": "state", "expect": {"fields": ["X"]}},
    {"op": "state", "expect": {"editable": "X"}},
    {"op": "state", "expect": {"no_errors": "yes"}},
    {"op": "state", "expect": {"grid": {"rows": -1}}},
    {"op": "state", "expect": {"grid": {"contains": ["X"]}}},
    {"op": "state", "save": []},
    {"op": "state", "save": {"x": {"from": "caption", "regex": "["}}},
    {"op": "state", "save": {"x": {"from": "grid:X", "row": -1}}},
    {"op": "state", "save": {"x": {"from": "caption", "extra": True}}},
    {"op": "state", "save": {"x": "unsupported"}},
    {"op": "press", "name": "${unknown}"},
    {"op": "state", "note": 4},
]


@pytest.fixture
def web():
    client = WebClient(Config(url="https://fixture.invalid"), budget=2)
    try:
        yield client
    finally:
        client.shutdown()


@pytest.mark.parametrize("bad", WEB_BAD)
def test_web_refuses_invalid_later_step_without_effect(web, tmp_path, bad):
    effects = []
    web._op_press = lambda *args: effects.append(args) or {"scope": "form"}
    server = create_server(web, tmp_path)
    error, result = call(server, "web_run_scenario",
                         {"steps": [{"op": "press", "name": "SyntheticPost"}, bad]})
    assert error and result["code"] == "refused"
    assert effects == [] and web.runs.get("browser") is None


WHS_BAD = [
    {"press": []}, {"values": []}, {"values": {"X": {}}},
    {"expect": []}, {"expect": {"fields": ["X"]}},
    {"expect": {"labels": "X"}}, {"expect": {"title": []}},
    {"note": {}}, {"expect": {"fields": {"X": []}}},
    {"values": {"X": "${unknown}"}},
]


@pytest.mark.parametrize("form", [False, True])
@pytest.mark.parametrize("bad", WHS_BAD)
def test_whs_refuses_before_backend_effect(web, tmp_path, form, bad):
    effects = []
    if form:
        backend = FormBackend(web)
        backend.tabs["W1"] = object()
        backend._op_submit = lambda *args: effects.append(args) or page_from_form(FORM_S1)
        server = create_whs_server(backend, tmp_path)
        before = 0
    else:
        fake = FakeAos("login.xml", "menu-main.xml")
        server = server_for(fake, tmp_path)
        error, _ = call(server, "whs_login", {"worker": "W1", "password": "fixture"})
        assert not error
        before = len(fake.requests)
    error, result = call(server, "whs_run_scenario", {"worker": "W1",
                         "steps": [{"press": "Inbound"}, bad]})
    assert error and result["code"] == "refused"
    assert effects == [] and web.runs.get("browser") is None
    if not form:
        assert len(fake.requests) == before


def test_web_saved_values_remain_runtime_dependencies(web, tmp_path):
    effects = []
    web._op_press = lambda *args: {"scope": "form", "caption": "PO-1"}
    web._op_set = lambda pairs: effects.append(pairs) or {"scope": "form"}
    web._js = lambda *args: {}
    server = create_server(web, tmp_path)
    error, result = call(server, "web_run_scenario", {"steps": [
        {"op": "press", "name": "Read", "save": {"order": "caption"}},
        {"op": "set", "values": {"Order": "${order}"}}]})
    assert not error and result["ok"]
    assert effects == [[("Order", "PO-1")]]


@pytest.mark.parametrize("saved, ok", [("2", True), ("not-an-index", False)])
def test_numeric_page_dependency_checked_before_dependent_step(web, tmp_path, saved, ok):
    effects = []
    web._op_press = lambda *args: {"scope": "form", "caption": saved}
    web._op_select_row = lambda *args: effects.append(args) or {"scope": "form"}
    web._js = lambda *args: {}
    server = create_server(web, tmp_path)
    error, result = call(server, "web_run_scenario", {"steps": [
        {"op": "press", "name": "Read", "save": {"row": "caption"}},
        {"op": "select_row", "row": "${row}"}]})
    assert not error and result["ok"] == ok
    assert effects == [("", 2, None)] if ok else effects == []
    if not ok:
        assert result["steps"][1]["error"] == "refused"


def test_missing_saved_expectation_reports_runtime_error_without_second_action(web, tmp_path):
    effects = []
    web._op_press = lambda *args: effects.append(args) or {"scope": "form"}
    web._js = lambda *args: {}
    server = create_server(web, tmp_path)
    error, result = call(server, "web_run_scenario", {"stop_on_mismatch": False, "steps": [
        {"op": "press", "name": "Read", "save": {"order": "field:Missing"}},
        {"op": "press", "name": "Second", "expect": {"caption": "${order}"}}]})
    assert not error and not result["ok"]
    assert len(effects) == 1 and result["steps"][1]["error"] == "refused"


def test_web_runtime_failure_keeps_prior_transcript(web, tmp_path):
    def press(name, nth):
        if name == "Missing":
            raise D365WebError("not_found", "synthetic page-dependent failure")
        return {"scope": "form"}
    web._op_press = press
    server = create_server(web, tmp_path)
    error, result = call(server, "web_run_scenario", {"steps": [
        {"op": "press", "name": "Read"}, {"op": "press", "name": "Missing"}]})
    assert not error and not result["ok"]
    assert len(result["steps"]) == 2 and result["steps"][1]["error"] == "not_found"
