"""Logoff discovery against fake OData, the app protocol and signed-in page responses.

No D365 environment is contacted. A real D365 page must accept the fetch before live acceptance.
"""
import json
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from boring_pondnt.d365_entra import Response
from boring_pondnt.d365whsapp_backends import FormBackend, load_backend
from boring_pondnt.d365whsapp_client import WhsClient, WhsError
from boring_pondnt.d365whsapp_protocol import parse_page
from boring_pondnt.d365web_client import D365WebError
from boring_pondnt.d365web_whsform import page_from_form
from boring_pondnt.whs_logoff import LogoffItems, LookupError, MAX_BYTES, lookup_path, names_from_reply
from boring_pondnt.whs_steps import exit_button
from test_d365whsapp_client import CONFIG, FakeAos, sample
from test_d365whsapp_server import FORM_LOGIN, call, server_for


ROWS = {"value": [{"MenuItemName": "Abmelden", "Title": "QUIT SESSION"}]}


def app(fake, buttons=()):
    return WhsClient(replace(CONFIG, logoff_buttons=list(buttons)), fake, lambda: "s3cret")


def custom_menu(client, worker="W1", name="ExitConfigured", label="quit session"):
    client.login(worker, "1")
    client.session(worker).page = parse_page(
        sample("menu-main.xml").replace('name="Log off"', f'name="{name}"')
        .replace('label="Log off"', f'label="{label}"'))


@pytest.mark.parametrize("name,label", [("ExitConfigured", "quit session"), ("ABMELDEN", "Ende")])
def test_app_uses_activity_name_or_title_and_existing_credentials(name, label):
    fake = FakeAos("login.xml", "menu-main.xml", "login.xml")
    fake.lookup_reply = Response(200, json.dumps(ROWS))
    c = app(fake)
    custom_menu(c, name=name, label=label)
    assert c.logoff("W1").is_login
    sent = parse_page(fake.requests[-1]["_xml"])
    assert sent.control(name).data == "1"
    request, = fake.lookup_requests
    query = parse_qs(urlsplit(request["url"]).query)
    assert query == {"cross-company": ["true"], "$select": ["MenuItemName,Title"],
                     "$filter": ["ActivityCode eq Microsoft.Dynamics.DataEntities.WHSWorkActivity'LogOff'"
                                 " and dataAreaId eq 'usmf'"]}
    assert request["body"] is None and request["allow_redirects"] is False
    assert request["max_bytes"] == MAX_BYTES
    # Separate token caches for the mobile-service and OData audiences, same Entra client/secret.
    assert fake.token_calls == 2


def test_app_failure_falls_back_after_one_fresh_logoff_read_per_company(tmp_path):
    fake = FakeAos(*(["login.xml", "menu-main.xml", "login.xml"] * 2))
    fake.lookup_reply = Response(403, '{"secret":"must not reach the log"}')
    server = server_for(fake, tmp_path)
    for worker in ("W1", "W2"):
        assert not call(server, "whs_login", {"worker": worker, "password": "1"})[0]
        assert not call(server, "whs_logoff", {"worker": worker})[0]
    assert len(fake.lookup_requests) == 2
    logs = "\n".join(p.read_text() for p in (tmp_path / "whs-steps").glob("*.log"))
    assert logs.count("logoff lookup WarehouseMobileDeviceMenuItemsV4") == 2
    assert "HTTP 403" in logs and "must not reach" not in logs


def test_app_config_works_when_entity_is_forbidden():
    fake = FakeAos("login.xml", "menu-main.xml", "login.xml")
    fake.lookup_reply = Response(403, "")
    c = app(fake, ["QUIT SESSION"])
    custom_menu(c)
    assert c.logoff("W1").is_login


def test_app_stuck_reports_all_candidates_and_visible_buttons():
    fake = FakeAos("login.xml", "menu-main.xml")
    fake.lookup_reply = Response(200, json.dumps(ROWS))
    c = app(fake, ["Another exit"])
    custom_menu(c, name="Continue", label="Weiter")
    with pytest.raises(WhsError) as raised:
        c.logoff("W1")
    assert raised.value.code == "stuck"
    for text in ("ActivityCode LogOff", "Abmelden", "QUIT SESSION", "Another exit",
                 "Log off", "Leave", "Continue", "Weiter"):
        assert text in raised.value.message


class SignedInPage:
    def __init__(self, status=200, rows=ROWS, company="USMF"):
        self.status, self.rows, self.company = status, rows, company
        self.expressions = []
        self.pending = {}

    def url(self):
        return f"https://aos.example/?cmp={self.company}&mi=action:WHSWorkExecute"

    def evaluate(self, expression, **options):
        self.expressions.append((expression, options))
        if "const id =" in expression:
            ident = re.search(r'const id = "([^"]+)"', expression)[1]
            if "fetch(" in expression:
                self.pending[ident] = {"done": True, "reply": {"status": self.status, "text": json.dumps(self.rows)}}
                return ident
            return self.pending.pop(ident, {"missing": True})
        return {"status": self.status, "text": json.dumps(self.rows)}


def form(page, buttons=(), label="quit session"):
    web = SimpleNamespace(page=page, config=SimpleNamespace(company="wrong-default"))
    backend = FormBackend(web, list(buttons))
    menu = page_from_form({"caption": "Menu", "controls": [
        {"name": "ExitConfigured", "role": "CommandButton", "text": label},
        {"name": "Back", "role": "CommandButton", "text": "Back"}]})
    login = page_from_form(FORM_LOGIN)
    pages, pressed = [menu, login], []
    backend._page = lambda: pages.pop(0) if len(pages) > 1 else pages[0]
    backend._press = pressed.append
    backend._logoff_worker = "W1"
    return backend, pages, pressed


def test_form_fetch_from_signed_in_page_finds_custom_title_and_caches():
    page = SignedInPage()
    backend, pages, pressed = form(page)
    assert backend._walk_out()["page"] == "Login"
    assert pressed == ["ExitConfigured"]
    expression, options = page.expressions[0]
    assert "fetch(" in expression and "credentials: 'same-origin'" in expression
    assert "method: 'GET'" in expression and "redirect: 'error'" in expression
    assert "Authorization" not in expression and not options.get("await_promise")
    assert json.dumps(lookup_path("USMF")) in expression
    # Same company with different casing: no second lookup; a different company does fetch.
    page.company = "usmf"
    assert backend.logoff_items.get(backend._company(), "W2", backend._logoff_names).names
    assert sum("fetch(" in expression for expression, _ in page.expressions) == 1
    page.company = "DEMF"
    backend.logoff_items.get(backend._company(), "W2", backend._logoff_names)
    assert sum("fetch(" in expression for expression, _ in page.expressions) == 2


@pytest.mark.parametrize("configured,label,expected", [
    ([], "Log off", "ExitConfigured"), (["QUIT SESSION"], "quit session", "ExitConfigured"),
])
def test_form_forbidden_entity_uses_config_or_english_fallback(configured, label, expected):
    backend, _, pressed = form(SignedInPage(status=403), configured, label)
    assert backend._walk_out()["page"] == "Login"
    assert pressed == [expected]
    assert "HTTP 403" in backend.logoff_notes("W1")[0]
    assert backend.logoff_notes("W1") == []


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable for the fake fetch runtime")
@pytest.mark.parametrize("reply,expected", [
    ("new Response(JSON.stringify({value: [{Title: 'QUIT SESSION'}]}))", {"status": 200}),
    ("new Response('denied', {status: 403})", {"status": 403, "text": ""}),
    ("new Response('x'.repeat(262145))", {"status": 200, "truncated": True}),
    ("Promise.reject(new Error('synthetic network failure'))",
     {"status": 0, "text": "", "error": "Error"}),
])
def test_page_side_javascript_runs_against_fake_fetch(reply, expected):
    # Execute the exact emitted expression, including streaming/byte limits and error handling.
    # This is a JS runtime with fake fetch, not real browser or D365 acceptance.
    page = SignedInPage()
    backend, _, _ = form(page)
    backend._logoff_names("USMF")
    expression = page.expressions[0][0]
    script = """
        globalThis.fetch = async (path, options) => {
            if (path !== %s || options.method !== 'GET' ||
                options.credentials !== 'same-origin' || options.redirect !== 'error' ||
                options.headers.Authorization !== undefined)
                throw new Error('incorrect request');
            return %s;
        };
        %s.then(result => console.log(JSON.stringify(result)));
    """ % (json.dumps(lookup_path("USMF")), reply, expression)
    result = subprocess.run([shutil.which("node"), "--eval", script],
                            capture_output=True, text=True, timeout=15, check=True)
    answer = json.loads(result.stdout)
    assert {key: answer[key] for key in expected} == expected
    if answer["status"] == 200 and not answer.get("truncated"):
        assert names_from_reply(answer["status"], answer["text"]) == ("QUIT SESSION",)


def test_form_stuck_has_the_same_diagnostics():
    backend, pages, _ = form(SignedInPage())
    pages[:] = [page_from_form({"caption": "Menu", "controls": [
        {"name": "Weiter", "role": "CommandButton", "text": "Continue"}]})]
    with pytest.raises(D365WebError) as raised:
        backend._walk_out()
    assert raised.value.code == "stuck"
    for text in ("Abmelden", "QUIT SESSION", "Log off", "Continue", "Weiter"):
        assert text in raised.value.message
    assert raised.value.details["page"]["buttons"] == ["Weiter"]


def test_verified_inbound_leave_document_order_with_activity_names():
    names = ("Abmelden", "QUIT SESSION")
    assert exit_button([("OK", ""), ("DemoLeaveDocument", "Leave document")], names) == "DemoLeaveDocument"
    assert exit_button([("DemoBackToDocument", "Back"), ("DemoConfirmLeave", "Leave document")],
                       names) == "DemoConfirmLeave"
    assert exit_button([("Back", ""), ("Quit", "QUIT SESSION")], names) == "Quit"
    # Exact equality only; a work button containing a logoff name is not an exit.
    assert exit_button([("OK", ""), ("Search", "QUIT SESSION records")], names) is None


@pytest.mark.parametrize("mode", ["app", "form"])
def test_top_level_config_is_optional_validated_and_loaded(tmp_path, mode):
    path = tmp_path / "whs.json"
    data = ({"mode": mode, **CONFIG.__dict__} if mode == "app" else
            {"mode": mode, "form": {"url": "https://aos.example"}})
    data.pop("logoff_buttons", None)
    path.write_text(json.dumps(data))
    backend = load_backend(path)
    items = backend.client.logoff_items if mode == "app" else backend.logoff_items
    assert items.buttons == []
    data["logoff_buttons"] = ["QUIT SESSION"]
    path.write_text(json.dumps(data))
    backend = load_backend(path)
    items = backend.client.logoff_items if mode == "app" else backend.logoff_items
    assert items.buttons == ["QUIT SESSION"]
    for bad in ("QUIT SESSION", [1], [""], None):
        data["logoff_buttons"] = bad
        path.write_text(json.dumps(data))
        with pytest.raises(ValueError, match="logoff_buttons"):
            load_backend(path)


@pytest.mark.parametrize("status,text,cut", [
    (403, "not included", False), (200, "{}", False), (200, "not JSON", False),
    (200, '{"value": [], "@odata.nextLink": "https://other.invalid"}', False),
    (200, '{"value": []}', True), (200, '{"value": [{"Title": 42}]}', False),
])
def test_discovery_refuses_incomplete_or_malformed_replies(status, text, cut):
    with pytest.raises(LookupError):
        names_from_reply(status, text, cut)


def test_draining_notes_does_not_block_a_pending_form_operation():
    items = LogoffItems()
    started, release = threading.Event(), threading.Event()

    def read(company):
        started.set()
        assert release.wait(5)
        raise LookupError("synthetic failure")

    with ThreadPoolExecutor(max_workers=2) as pool:
        lookup = pool.submit(items.get, "usmf", "W1", read)
        try:
            assert started.wait(2)
            # act() drains notes even when the browser budget expires and returns step_running.
            # It must let the caller collect later instead of waiting for the network operation.
            assert pool.submit(items.notes, "W1").result(timeout=1) == []
        finally:
            release.set()
        lookup.result(timeout=2)
    assert len(items.notes("W1")) == 1


def test_empty_items_and_failures_are_cached_once_per_company():
    items, reads = LogoffItems(), []
    read = lambda company: reads.append(company) or ()
    assert items.get("usmf", "W1", read).failure == "no LogOff items returned"
    items.get("USMF", "W2", read)
    items.get("demf", "W2", read)
    assert reads == ["usmf", "demf"]
    assert len(items.notes("W1")) == len(items.notes("W2")) == 1
    assert "O''Brien" in parse_qs(urlsplit(lookup_path("O'Brien")).query)["$filter"][0]


def test_failed_lookups_are_retried_after_a_while_successes_stay_cached():
    """Live on a dev box: one HTTP 400 right after a restart kept logoff stuck for the backend's life."""
    from boring_pondnt.whs_logoff import RETRY_FAILED_SECONDS
    now = [0.0]
    items, reads = LogoffItems(clock=lambda: now[0]), []
    replies = [LookupError("HTTP 400"), ("ZZ ENDE",)]
    def read(company):
        reads.append(company)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply
    assert items.get("usrt", "W1", read).failure == "HTTP 400"
    now[0] = RETRY_FAILED_SECONDS - 1
    assert items.get("usrt", "W1", read).failure == "HTTP 400" and reads == ["usrt"]
    now[0] = RETRY_FAILED_SECONDS + 1
    assert items.get("usrt", "W1", read).names == ("ZZ ENDE",) and reads == ["usrt", "usrt"]
    now[0] = 10 * RETRY_FAILED_SECONDS
    assert items.get("usrt", "W1", read).names == ("ZZ ENDE",) and reads == ["usrt", "usrt"]


def test_page_side_failure_names_the_browser_error():
    """A cloud-hosted box failed where the console fetch worked; the answer must say what failed."""
    class FailingPage(SignedInPage):
        def evaluate(self, expression, **options):
            self.expressions.append((expression, options))
            return {"status": 0, "text": "", "error": "TypeError"}
    backend, _, _ = form(FailingPage())
    with pytest.raises(LookupError) as raised:
        backend._logoff_names("200")
    for part in ("page-side OData request failed", "TypeError"):
        assert part in str(raised.value)
