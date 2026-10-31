"""d365whsapp MCP tools over a fake AOS (recorded pages)."""

from __future__ import annotations

import asyncio
import json

import pytest

from boring_pondnt.d365whsapp_client import WhsClient
from boring_pondnt.d365whsapp_server import create_server
from test_d365whsapp_client import CONFIG, FakeAos, sample


def call(server, name, args=None):
    result = asyncio.run(server.call_tool(name, args or {}))
    blocks = result[0] if isinstance(result, tuple) else result
    content = blocks.content if hasattr(blocks, "content") else blocks
    text = content[0].text
    assert "s3cret" not in text
    return getattr(blocks, "is_error", False), json.loads(text)


def server_for(fake, tmp_path, **kw):
    from dataclasses import replace
    client = WhsClient(replace(CONFIG), transport=fake, secret=lambda: "s3cret")
    return create_server(client, tmp_path, log_calls=True, **kw)


def test_tools_published(tmp_path):
    server = server_for(FakeAos(), tmp_path)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {"whs_login", "whs_screen", "whs_submit", "whs_logoff", "whs_run_scenario", "whs_wait", "whs_log"}


def test_login_submit_screen(tmp_path):
    server = server_for(FakeAos("login.xml", "menu-main.xml", "menu-inbound.xml"), tmp_path)
    error, page = call(server, "whs_login", {"worker": "whsapp01", "password": "1"})
    assert not error and page["page"] == "Menu" and page["worker"] == "WHSAPP01" and not page["resumed"]
    error, page = call(server, "whs_submit", {"worker": "WHSAPP01", "press": "Inbound"})
    assert not error and page["title"] == "Inbound" and "Purchase Receive" in page["buttons"]
    error, screen = call(server, "whs_screen", {"worker": "WHSAPP01"})
    assert not error and screen == page
    log = (tmp_path / "calls-d365whsapp.jsonl").read_text()
    assert '"1"' not in log and "password" not in log  # argument text is never logged


def test_refusals_and_failures(tmp_path):
    status, body = sample("error-tampering.txt").split(" ", 2)[1:]
    server = server_for(FakeAos("login.xml", "menu-main.xml", (int(status), body)), tmp_path)
    error, outcome = call(server, "whs_screen", {"worker": "WHSAPP01"})
    assert error and outcome["status"] == "refused" and outcome["code"] == "no_session"
    call(server, "whs_login", {"worker": "WHSAPP01", "password": "1"})
    error, outcome = call(server, "whs_submit", {"worker": "WHSAPP01", "values": {"Main": "x"}})
    assert error and outcome["status"] == "refused" and "not an input field" in outcome["message"]
    assert outcome["details"]["page"]["page"] == "Menu"
    error, outcome = call(server, "whs_submit", {"worker": "WHSAPP01", "press": "Inbound"})
    assert error and outcome["status"] == "failed" and outcome["code"] == "REQUEST_XML_TAMPERING"


def test_scenario_with_expectations(tmp_path):
    fake = FakeAos("login.xml", "menu-main.xml", "menu-inbound.xml", "poreceive-ponum.xml", "poreceive-ponum-error.xml")
    server = server_for(fake, tmp_path)
    call(server, "whs_login", {"worker": "WHSAPP01", "password": "1"})
    error, run = call(server, "whs_run_scenario", {"worker": "WHSAPP01", "steps": [
        {"press": "Inbound", "expect": {"page": "Menu", "title": "Inbound"}},
        {"press": "Purchase Receive", "expect": {"step": "PONum"}},
        {"values": {"PONum": "00000050"}, "press": "OK", "expect": {"step": "ItemId"}},
        {"press": "Cancel"},
    ]})
    assert not error and run["ok"] is False
    assert [s.get("mismatch") is None for s in run["steps"]] == [True, True, False]  # stopped at step 3
    assert run["steps"][2]["message"]["kind"] == "error"
    assert run["final"]["step"]["id"] == "PONum"


def test_scenario_refuses_bad_steps_before_running(tmp_path):
    fake = FakeAos("login.xml", "menu-main.xml")
    server = server_for(fake, tmp_path)
    call(server, "whs_login", {"worker": "WHSAPP01", "password": "1"})
    error, outcome = call(server, "whs_run_scenario", {"worker": "WHSAPP01", "steps": [{"press": "Inbound", "click": 1}]})
    assert error and outcome["status"] == "refused" and "unknown keys" in outcome["message"]
    assert len(fake.requests) == 2


def test_call_budget_caps_the_step_timeout(tmp_path):
    server = server_for(FakeAos(), tmp_path, call_budget=60.0)
    assert server._d365whsapp_client.config.step_timeout == 55.0


def test_scenario_variables_and_notes(tmp_path):
    fake = FakeAos("login.xml", "menu-main.xml", "menu-inbound.xml")
    server = server_for(fake, tmp_path)
    call(server, "whs_login", {"worker": "WHSAPP01", "password": "1"})
    error, result = call(server, "whs_run_scenario", {"worker": "WHSAPP01", "variables": {"menu": "Inbound"},
                                                       "steps": [{"press": "${menu}", "note": "to Inbound",
                                                                  "expect": {"title": "${menu}"}}]})
    assert not error and result["ok"] and result["steps"][0]["page"] == "Menu"
    error, outcome = call(server, "whs_run_scenario", {"worker": "WHSAPP01", "variables": {"a": "1"},
                                                        "steps": [{"press": "${b}"}]})
    assert error and outcome["status"] == "refused" and "unknown variable" in outcome["message"]


class SlowAos(FakeAos):
    def __call__(self, url, headers, body, timeout):
        import time
        if "login.microsoftonline.com" not in url and self.requests:  # login is quick, steps are slow
            time.sleep(0.25)
        return super().__call__(url, headers, body, timeout)


def test_long_scenario_answers_running_and_is_collected(tmp_path):
    fake = SlowAos("login.xml", "menu-main.xml", "menu-inbound.xml", "poreceive-ponum.xml", "poreceive-ponum.xml")
    server = server_for(fake, tmp_path, call_budget=5.3)  # 0.3 s to answer within, like 25 s of a 30 s Pond call
    call(server, "whs_login", {"worker": "WHSAPP01", "password": "1"})
    error, run = call(server, "whs_run_scenario", {"worker": "WHSAPP01", "steps": [
        {"press": "Inbound", "note": "n1"}, {"press": "Purchase Receive"}, {"press": "Cancel"}]})
    assert not error and run["status"] == "running" and run["worker"] == "WHSAPP01" and run["steps_done"] <= 2
    error, outcome = call(server, "whs_submit", {"worker": "WHSAPP01", "press": "OK"})
    assert error and outcome["code"] == "busy" and "whs_wait" in outcome["message"]
    error, outcome = call(server, "whs_run_scenario", {"worker": "WHSAPP01", "steps": [{"press": "OK"}]})
    assert error and outcome["code"] == "busy"
    result = run
    for _ in range(20):
        error, result = call(server, "whs_wait", {"worker": "WHSAPP01", "seconds": 0.5})
        if result.get("status") != "running":
            break
    assert not error and result["ok"] is True and len(result["steps"]) == 3 and result["steps"][0]["note"] == "n1"
    error, outcome = call(server, "whs_wait", {"worker": "WHSAPP01"})
    assert error and outcome["code"] == "no_run"  # collected once


def test_short_scenario_answers_directly(tmp_path):
    fake = FakeAos("login.xml", "menu-main.xml", "menu-inbound.xml")
    server = server_for(fake, tmp_path, call_budget=30)
    call(server, "whs_login", {"worker": "WHSAPP01", "password": "1"})
    error, run = call(server, "whs_run_scenario", {"worker": "WHSAPP01", "steps": [{"press": "Inbound"}]})
    assert not error and run["ok"] is True and "status" not in run
    error, outcome = call(server, "whs_wait", {"worker": "WHSAPP01"})
    assert error and outcome["code"] == "no_run"


# --- Review finding F2: collectors and admission ---------------------------------------------------------------

def test_an_old_collector_does_not_remove_a_newer_run():
    """Two collectors wait for run A; one collects it, run B starts; the late collector of A
    returns A and leaves B registered (the worker stays busy for B)."""
    import threading
    from boring_pondnt.d365whsapp_server import ScenarioRuns
    from boring_service_kit.outcomes import Refused
    runs = ScenarioRuns(budget=None)
    a_go, b_go = threading.Event(), threading.Event()
    entry_started = threading.Event()

    def run_a(transcript):
        entry_started.set()
        a_go.wait(5)
        return {"run": "A"}
    first = {}
    t1 = threading.Thread(target=lambda: first.update(r=runs.start("w1", run_a)))
    t1.start()
    assert entry_started.wait(5)
    with runs.runs.lock:
        entry_a = runs.runs.peek("W1")
    late = {}
    late_gate = threading.Event()
    original = runs._answer

    def slow_answer(key, entry, limit):  # a collector that took A's reference, delayed until B runs
        entry.future.result(timeout=5)
        late_gate.wait(5)
        return original(key, entry, limit)
    t2 = threading.Thread(target=lambda: late.update(r=slow_answer("W1", entry_a, 5)))
    t2.start()
    a_go.set()
    t1.join(5)
    assert first["r"] == {"run": "A"}
    b_started = threading.Event()

    def run_b(transcript):
        b_started.set()
        b_go.wait(5)
        return {"run": "B"}
    runs.budget = 0.05
    assert runs.start("w1", run_b)["status"] == "running"
    late_gate.set()
    t2.join(5)
    assert late["r"] == {"run": "A"}  # the same result for a collector of the same run
    assert runs.runs.get("W1") not in (None, entry_a)  # B is still registered
    try:
        with runs.acting("w1"):
            raise AssertionError("acting while B runs")
    except Refused as refused:
        assert refused.code == "busy"
    b_go.set()
    runs.budget = None
    assert runs.wait("w1", 5) == {"run": "B"}


def test_a_scenario_cannot_start_while_an_acting_call_runs():
    import threading
    from boring_pondnt.d365whsapp_server import ScenarioRuns
    from boring_service_kit.outcomes import Refused
    runs = ScenarioRuns(budget=None)
    inside, leave = threading.Event(), threading.Event()

    def manual():
        with runs.acting("w1"):
            inside.set()
            leave.wait(5)
    t = threading.Thread(target=manual)
    t.start()
    assert inside.wait(5)
    try:
        runs.start("w1", lambda transcript: {"ok": True})
        raise AssertionError("scenario admitted during a manual step")
    except Refused as refused:
        assert refused.code == "busy" and "step in progress" in refused.message
    leave.set()
    t.join(5)
    assert runs.start("w1", lambda transcript: {"ok": True}) == {"ok": True}
    assert not runs.active


def test_tool_schemas_are_strict_for_copilot(tmp_path):
    from strict_schema import assert_copilot_accepts
    assert_copilot_accepts(asyncio.run(server_for(FakeAos(), tmp_path).list_tools()))


# --- mode form (D38): the same tools over the in-form simulator, through d365web's WebClient ----------

class FakeWeb:
    """Stands in for WebClient and the form backend's browser operations: form pages as
    d365web_whsform reads them (form_server binds them in place of FormBackend's _op_*)."""

    def __init__(self, *pages):
        from types import SimpleNamespace
        from boring_pondnt import d365web_whsform as whsform
        self.pages = [whsform.page_from_form(p) for p in pages]
        self.whs_tabs: dict = {}
        self.runs = SimpleNamespace(get=lambda key: None)  # every fake step answers at once
        self.config = SimpleNamespace(step_timeout=600.0)
        self.budget = 25.0
        self.calls: list = []

    def settle(self):
        pass

    def call(self, label, fn, budget=None, owner=None):
        self.budget_given = budget
        return fn()

    def _next(self, *call):
        self.calls.append(call)
        return self.pages.pop(0)

    def whs_login(self, worker, password, company=None):
        if password == "wrong":
            from boring_pondnt.d365web_client import D365WebError
            raise D365WebError("login_failed", "login refused")
        self.whs_tabs[worker.upper()] = object()
        return self._next("login", worker, company)

    def whs_submit(self, worker, values=None, press=None):
        return self._next("submit", worker, values, press, self.budget_given)

    def whs_screen(self, worker):
        return self._next("screen", worker)

    def whs_logoff(self, worker):
        self.whs_tabs.pop(worker.upper(), None)
        return self._next("logoff", worker)


FORM_MENU = {"caption": "Menu", "controls": [{"name": "MenuLabel", "role": "Label", "text": "Main Menu"},
                                             {"name": "Inbound", "role": "CommandButton", "text": "Inbound"},
                                             {"name": "Log off", "role": "CommandButton", "text": "Log off"}]}
FORM_S1 = {"caption": "Demo receiving", "controls": [
    {"name": "error", "role": "Label", "text": "No inbound document found"},
    {"name": "DemoPageS1", "role": "Label", "text": "Scan document or license plate"},
    {"name": "DemoScanId", "role": "Input", "value": "", "label": "Scan document or license plate"},
    {"name": "OK", "role": "CommandButton", "text": "OK"}]}
FORM_LOGIN = {"caption": "Login", "controls": [{"name": "UserId", "role": "Input", "value": ""},
                                               {"name": "Password", "role": "Input", "value": ""}]}


def form_server(tmp_path, *pages, **kw):
    from boring_pondnt.d365whsapp_backends import FormBackend
    web = FakeWeb(*pages)
    backend = FormBackend(web)
    backend.tabs = web.whs_tabs
    backend._op_login, backend._op_submit = web.whs_login, web.whs_submit
    backend._op_screen, backend._op_logoff = web.whs_screen, web.whs_logoff
    return web, create_server(backend, tmp_path, log_calls=True, **kw)


def test_both_modes_publish_the_same_tools_and_say_which_answers(tmp_path):
    app = server_for(FakeAos(), tmp_path / "app")
    _, form = form_server(tmp_path / "form")
    tools = lambda s: {t.name: t.input_schema for t in asyncio.run(s.list_tools())}
    assert tools(app) == tools(form)
    assert "Mode app" in app.instructions and "Mode form" in form.instructions and "not_checked" in form.instructions


def test_form_mode_results_carry_mode_and_not_checked(tmp_path):
    web, server = form_server(tmp_path, FORM_MENU, FORM_S1, FORM_S1, FORM_LOGIN, call_budget=30)
    error, page = call(server, "whs_login", {"worker": "whsapp05", "password": "1"})
    assert not error and page == {"mode": "form", "worker": "WHSAPP05", "resumed": False, "page": "Menu",
                                  "caption": "Menu", "title": "Main Menu", "buttons": ["Inbound", "Log off"],
                                  "labels": {"MenuLabel": "Main Menu"}}
    error, run = call(server, "whs_run_scenario", {"worker": "WHSAPP05", "steps": [
        {"press": "Inbound", "expect": {"page": "Default", "step": "DemoPageS1", "message_kind": "error"}},
        {"values": {"DemoScanId": "X"}, "press": "OK", "expect": {"message": "no inbound", "message_kind": "info"}}]})
    assert not error and run["mode"] == "form" and run["ok"] is True
    assert run["not_checked"] == ["message_kind (the form shows info and error messages alike)",
                                  "page (pattern of work pages)"]
    assert run["steps"][0] == {"step": 1, "page": "work", "title": "Demo receiving",
                               "message": {"text": "No inbound document found"}}
    assert web.calls[1][-1] == 600.0  # a run's step waits as long as the form may take, in its own thread
    error, out = call(server, "whs_logoff", {"worker": "WHSAPP05"})
    assert not error and out == {"mode": "form", "worker": "WHSAPP05", "logged_off": True}
    error, outcome = call(server, "whs_run_scenario", {"worker": "WHSAPP05", "steps": [{"press": "OK"}]})
    assert error and outcome["code"] == "no_session"
    error, outcome = call(server, "whs_login", {"worker": "WHSAPP05", "password": "wrong"})
    assert error and outcome["code"] == "login_failed"
    log = (tmp_path / "whs-steps" / "steps-WHSAPP05.log").read_text()
    assert '-> work "Demo receiving" (form)' in log and "[DemoScanId]: X" in log and "error login_failed" in log


def test_app_mode_results_carry_mode(tmp_path):
    server = server_for(FakeAos("login.xml", "menu-main.xml"), tmp_path)
    error, page = call(server, "whs_login", {"worker": "whsapp01", "password": "1"})
    assert not error and page["mode"] == "app"


def test_config_chooses_the_mode(tmp_path):
    from boring_pondnt.d365whsapp_backends import AppBackend, FormBackend, load_backend
    path = tmp_path / "d365whsapp.json"
    path.write_text('{"aos_url": "https://aos.example", "tenant": "t", "client_id": "c",'
                    ' "form": {"url": "https://aos.example"}}', encoding="utf-8-sig")
    assert isinstance(load_backend(path), AppBackend)  # app is the default, a form block alone changes nothing
    path.write_text('{"mode": "form", "form": {"url": "https://aos.example/x", "debug_port": 9334}}')
    backend = load_backend(path)
    assert isinstance(backend, FormBackend) and backend.web.config.profile == "d365whsapp-form"
    assert backend.web.config.url == "https://aos.example"
    backend.web.shutdown()
    for text, why in (('{"mode": "html"}', "mode must be one of"), ('{"mode": "form"}', "needs a form block"),
                      ('{"mode": "form", "form": {"url": "https://a.example", "nope": 1}}', "unknown keys"),
                      ('{"mode": "form", "form": {"url": "http://a.example"}}', "https")):
        path.write_text(text)
        with pytest.raises(ValueError, match=why):
            load_backend(path)


def test_form_mode_single_call_does_not_wait_past_its_budget_for_the_browser(tmp_path):
    web, server = form_server(tmp_path, FORM_MENU)
    call(server, "whs_login", {"worker": "WHSAPP05", "password": "1"})
    backend = server._d365whsapp_backend
    web.budget = 0.2
    with backend.lock:  # another worker's step holds the browser
        error, outcome = call(server, "whs_submit", {"worker": "WHSAPP05", "press": "Inbound"})
    assert error and outcome["code"] == "busy" and "another worker" in outcome["message"]
