"""Lookup races against synthetic app transport and page fetches on one browser executor."""
import json
import re
import shutil
import subprocess
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from boring_pondnt.d365_entra import Response
from boring_pondnt.d365web_client import Config as WebConfig, WebClient
from boring_pondnt.d365web_whsform import page_from_form
from boring_pondnt.d365whsapp_backends import AppBackend, FormBackend
from boring_pondnt.d365whsapp_protocol import parse_page
from boring_pondnt.d365whsapp_server import create_server
from boring_pondnt import whs_logoff
from boring_pondnt.whs_logoff import LOOKUP_SECONDS, LogoffItems, LookupError, safe_lookup_text
from test_d365whsapp_client import FakeAos, sample
from test_d365whsapp_server import FORM_LOGIN, call
from test_whs_logoff import ROWS, SignedInPage, app, form


GOOD = Response(200, json.dumps(ROWS))
BAD = Response(400, '{"token":"must-not-be-logged"}')


class Gate:
    def __init__(self):
        self.started = threading.Event()
        self.reply = Future()


@pytest.fixture(params=["app", "form"])
def race(request, tmp_path):
    gates = [Gate() for _ in range(3)]
    reads, threads = [], []
    lock = threading.Lock()
    def begin():
        with lock:
            gate = gates[len(reads)]
            reads.append(gate)
        gate.started.set()
        return gate

    web = None
    if request.param == "app":
        fake = FakeAos("login.xml", "menu-main.xml", "login.xml")
        def transport(url, headers, body, timeout, **options):
            answer = fake(url, headers, body, timeout, **options)
            if options.get("method") == "GET":
                return begin().reply.result(timeout=5)
            return answer
        client = app(transport)
        client.config.step_timeout = 5  # lookup gets its own 120 s, independent of warehouse steps
        backend = AppBackend(client)
        cache = client.logoff_items
        def lookup():
            return cache.get("usmf", "W1", client._logoff_names)
    else:
        web = WebClient(WebConfig(url="https://aos.example", company="usmf"), budget=1)
        backend = FormBackend(web)
        class Page:
            def __init__(self):
                self.pending = {}
            def url(self):
                return "https://aos.example/?cmp=usmf"
            def evaluate(self, expression, **options):
                threads.append(threading.current_thread().name)
                assert not options.get("await_promise"), "fetch must never block the browser queue"
                ident = re.search(r'const id = "([^"]+)"', expression)[1]
                if "fetch(" in expression:
                    assert options["timeout"] <= 2 and "120000" in expression
                    self.pending[ident] = begin()
                    return ident
                gate = self.pending.get(ident)
                if gate is None:
                    return {"missing": True}
                if not gate.reply.done():
                    return {"done": False}
                self.pending.pop(ident)
                reply = gate.reply.result()
                return {"done": True, "reply": {"status": reply.status, "text": reply.text}}
        page = Page()
        web.page = page
        @contextmanager
        def tab(worker, create=False):
            yield page
        backend._tab = tab
        backend._close_tab = lambda worker: None
        login_page = page_from_form(FORM_LOGIN)
        menu = page_from_form({"caption": "Menu", "controls": [
            {"name": "QUIT SESSION", "role": "CommandButton", "text": "QUIT SESSION"}]})
        current = [login_page]
        backend._page = lambda: current[0]
        backend._set = lambda values: None
        def press(name):
            current[0] = menu if name == "Login" else login_page
        backend._press = press
        web._js = lambda *args: True
        cache = backend.logoff_items
        def lookup():
            return web.executor.submit(lambda: cache.get(
                "usmf", "W1", backend._start_logoff_names,
                submit=lambda work: work(), pump=backend._poll_lookups)).result(timeout=2)

    server = create_server(backend, tmp_path)
    def login():
        error, answer = call(server, "whs_login", {"worker": "W1", "password": "1"})
        assert not error and answer["page"] == "Menu"
        assert gates[0].started.wait(2)
        if request.param == "app":
            backend.client.session("W1").page = parse_page(
                sample("menu-main.xml").replace('name="Log off"', 'name="QUIT SESSION"')
                .replace('label="Log off"', 'label="QUIT SESSION"'))
        else:
            # A hanging network fetch leaves this same executor available for other operations.
            assert web.executor.submit(lambda: threading.current_thread().name).result(timeout=1).startswith("d365web")
    def logs():
        return (tmp_path / "whs-steps/steps-W1.log").read_text()
    try:
        yield SimpleNamespace(gates=gates, reads=reads, cache=cache, login=login, lookup=lookup, backend=backend,
                              logoff=lambda: call(server, "whs_logoff", {"worker": "W1"}),
                              logs=logs, threads=threads, mode=request.param)
    finally:
        for gate in gates:
            if not gate.reply.done():
                gate.reply.set_result(BAD)
        if web is not None:
            web.executor.submit(backend._poll_lookups).result(timeout=2)
            assert all(name.startswith("d365web") for name in threads)
            web._close_browser = lambda: None
            web.shutdown()
        for attempt in list(cache._login.values()) + list(cache._logoff.values()):
            attempt.future.result(timeout=2)


def test_own_lookup_wins_without_waiting_for_a_hanging_login_lookup(race):
    race.login()
    race.gates[1].reply.set_result(GOOD)
    error, answer = race.logoff()
    assert not error and answer["logged_off"] is True
    assert not race.gates[0].reply.done() and len(race.reads) == 2
    assert "source logoff" in race.logs() and " ms; names/titles " in race.logs()
    race.gates[0].reply.set_result(BAD)
    race.cache._login["usmf"].future.result(timeout=2)
    assert race.lookup().names == ("Abmelden", "QUIT SESSION")  # late failure cannot poison success
    assert "source login" in race.logs() and "error HTTP 400" in race.logs()
    assert "must-not-be-logged" not in race.logs() and "https://" not in race.logs()


def test_login_lookup_wins_without_waiting_for_the_fresh_lookup(race):
    race.login()
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(race.logoff)
        assert race.gates[1].started.wait(2)
        race.gates[0].reply.set_result(GOOD)
        error, answer = result.result(timeout=2)
    assert not error and answer["logged_off"] is True
    assert not race.gates[1].reply.done() and len(race.reads) == 2
    race.gates[1].reply.set_result(BAD)
    race.cache._logoff["usmf"].future.result(timeout=2)
    assert race.lookup().names == ("Abmelden", "QUIT SESSION")


def test_failed_login_lookup_gets_one_immediate_fresh_logoff_lookup(race):
    race.gates[0].reply.set_result(BAD)
    race.login()
    assert race.cache._login["usmf"].future.result(timeout=2).failure == "HTTP 400"
    race.gates[1].reply.set_result(GOOD)
    error, answer = race.logoff()
    assert not error and answer["logged_off"] is True
    assert len(race.reads) == 2


def test_both_fail_and_cooldown_starts_at_the_fresh_failure_without_a_loop(race):
    now = [0.0]
    race.cache._clock = lambda: now[0]
    race.login()
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(race.logoff)
        assert race.gates[1].started.wait(2)
        now[0] = 10.0
        race.gates[1].reply.set_result(BAD)
        race.cache._logoff["usmf"].future.result(timeout=2)
        now[0] = 20.0
        race.gates[0].reply.set_result(BAD)
        error, answer = result.result(timeout=2)
    assert error and answer["code"] == "stuck" and len(race.reads) == 2
    now[0] = 69.9
    assert race.lookup().failure == "HTTP 400" and len(race.reads) == 2
    now[0] = 70.0
    race.gates[2].reply.set_result(GOOD)
    assert race.lookup().names == ("Abmelden", "QUIT SESSION") and len(race.reads) == 3
    now[0] = 1000.0
    assert race.lookup().names == ("Abmelden", "QUIT SESSION") and len(race.reads) == 3
    assert "source login" in race.logs() and "source logoff" in race.logs()


def test_lookup_timeout_covers_the_cold_request_in_both_modes():
    page = SignedInPage()
    backend, _, _ = form(page)
    backend._logoff_names("usmf")
    expression, options = page.expressions[0]
    assert LOOKUP_SECONDS == 120
    assert "120000" in expression and options["timeout"] >= 122
    fake = FakeAos()
    c = app(fake)
    c.config.step_timeout = 5
    c._logoff_names("usmf")
    assert 119 < fake.lookup_requests[0]["timeout"] <= 120


def test_completed_login_success_is_reused_without_a_fresh_lookup(race):
    race.gates[0].reply.set_result(GOOD)
    race.login()
    race.cache._login["usmf"].future.result(timeout=2)
    assert race.logoff()[0] is False
    assert len(race.reads) == 1


def test_prefetch_and_concurrent_logoffs_share_each_origin_once():
    cache = LogoffItems()
    login, fresh = Future(), Future()
    assert cache.prefetch("usmf", "W1", lambda _: login, lambda work: work()) is \
        cache.prefetch("USMF", "W2", lambda _: pytest.fail("second login"))
    reads = []
    def read(company):
        reads.append(company)
        return fresh
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cache.get, "usmf", "W1", read)
        second = pool.submit(cache.get, "USMF", "W2", read)
        fresh.set_result(("QUIT SESSION",))
        assert first.result(timeout=2).names == second.result(timeout=2).names
    assert len(reads) == 1
    login.set_exception(LookupError("HTTP 400"))


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable for fake page runtime")
def test_nonblocking_page_scripts_keep_two_fetches_independent():
    page = SignedInPage()
    backend, _, _ = form(page)
    backend._start_logoff_names("usmf")
    backend._start_logoff_names("demf")
    first_start, first_poll, second_start, second_poll = [e for e, _ in page.expressions]
    script = """
        (async () => {
            let resolveFirst, calls = 0;
            globalThis.fetch = async () => {
                calls++;
                if (calls === 1) return new Promise(resolve => {resolveFirst = resolve;});
                return new Response(JSON.stringify({value: [{Title: 'QUIT SESSION'}]}));
            };
            const first = eval(%s), second = eval(%s);
            await new Promise(resolve => setTimeout(resolve, 10));
            const own = eval(%s), pending = eval(%s);
            resolveFirst(new Response('private body', {status: 400}));
            await new Promise(resolve => setTimeout(resolve, 10));
            const login = eval(%s);
            console.log(JSON.stringify({own, pending, login, calls,
                left: Object.keys(globalThis.__pondWhsLogoffLookups).length}));
        })();
    """ % tuple(json.dumps(e) for e in (first_start, second_start, second_poll, first_poll, first_poll))
    result = subprocess.run([shutil.which("node"), "--eval", script], capture_output=True,
                            text=True, timeout=5, check=True)
    answer = json.loads(result.stdout)
    assert answer["own"]["done"] and answer["own"]["reply"]["status"] == 200
    assert answer["pending"] == {"done": False}
    assert answer["login"] == {"done": True, "reply": {"status": 400, "text": ""}}
    assert answer["calls"] == 2 and answer["left"] == 0


def test_lookup_notes_exclude_urls_and_credentials():
    cache = LogoffItems()
    cache.get("usmf", "W1", lambda _: ("QUIT SESSION", "https://fixture.invalid/?secret=opaque"))
    line, = cache.notes("W1")
    assert "source logoff" in line and "QUIT SESSION" in line and "https://" not in line and "opaque" not in line
    value = safe_lookup_text("TypeError https://fixture.invalid/body Bearer opaque token=hidden bsk_fixture")
    assert all(s not in value for s in ("fixture.invalid", "opaque", "hidden", "bsk_fixture"))


def test_a_cold_lookup_answers_retry_later_keeps_the_session_and_the_retry_logs_off(race, monkeypatch):
    # Wald, 2026-11-05: after a D365 cold start the lookup outlasts the call; logoff returns
    # retry_later at once, presses nothing and keeps the session, and the lookup goes on.
    monkeypatch.setattr(whs_logoff, "FOREGROUND_SECONDS", 0.3)
    race.login()
    error, answer = race.logoff()
    assert error and answer["code"] == "retry_later" and "call whs_logoff again" in answer["message"]
    assert race.gates[1].started.wait(2)  # logoff's own read is out and stays out
    if race.mode == "app":
        assert race.cache and backend_session_kept(race)
    race.gates[1].reply.set_result(GOOD)
    for _ in range(40):  # the background read lands in the cache
        if "usmf" in race.cache._cache:
            break
        threading.Event().wait(0.05)
    error, answer = race.logoff()
    assert not error and answer["logged_off"] is True
    assert "retry_later" in race.logs()


def backend_session_kept(race):
    return race.backend.client.session("W1") is not None


def test_an_own_failure_falls_back_without_waiting_for_a_hanging_login_read():
    items = LogoffItems()
    hanging: Future = Future()
    items.prefetch("usmf", "W1", lambda company: hanging)
    def failing(company):
        raise LookupError("HTTP 400")
    got = items.get("usmf", "W1", failing, wait=0.2)
    assert got.failure == "HTTP 400"  # rc2 behaviour on a failure: configured and English labels
    hanging.set_result(("Log off",))


def test_nothing_known_within_the_wait_raises_pending_and_the_read_goes_on():
    items = LogoffItems()
    slow: Future = Future()
    with pytest.raises(whs_logoff.LookupPending, match="retry_later"):
        items.get("usmf", "W1", lambda company: slow, wait=0.2)
    slow.set_result(("Log off",))
    assert items.get("usmf", "W1", lambda company: pytest.fail("cached"), wait=0.2).names == ("Log off",)


def test_an_unanswered_lookup_past_every_deadline_is_replaced():
    now = [0.0]
    cache = LogoffItems(clock=lambda: now[0])
    reads = []
    def read(company):
        future = Future()  # never answered
        reads.append(future)
        return future
    first = cache.prefetch("USMF", "W1", read, submit=lambda work: work())
    assert cache.prefetch("USMF", "W1", read, submit=lambda work: work()) is first and len(reads) == 1
    now[0] = LOOKUP_SECONDS + whs_logoff.STALE_MARGIN + 1
    assert cache.prefetch("USMF", "W1", read, submit=lambda work: work()) is not first and len(reads) == 2
