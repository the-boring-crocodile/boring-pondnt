"""The two ways d365whsapp reaches the warehouse app (D38), chosen by `mode` in d365whsapp.json.

app (the default): the mobile app protocol (d365whsapp_client), as the real device does it.
form: the in-form WHS app simulator (action:WHSWorkExecute) in the web client, in an Edge profile of
its own. FormBackend owns the form's warehouse behaviour (a tab per worker, login, the way out);
d365web's WebClient is the browser library it runs on. Needs no Entra app; context
classes may differ from the real app, and what the form cannot show is reported as not checked.
Never a silent fallback: the mode is configuration, and every result says which one answered.

Both backends hand the server a `Shown` page: the tool result, the whs_steps view the expectations
are checked against, and the step log's lines.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import Future
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from . import d365web_whsform as whsform
from .d365web_browser import Browser, BrowserError, Page as WebPage
from .collect import StillRunning
from .d365web_client import BROWSER, Config as WebConfig, D365WebError, WebClient
from .d365whsapp_client import Config as AppConfig, WhsClient, WhsError, view_of
from .d365whsapp_protocol import Page
from .d365whsapp_steplog import describe_page, page_values, secret
from .whs_steps import LOGOFF_STEPS_MAX, View, exit_button
from .whs_logoff import LogoffItems, LookupError, LookupPending, LOOKUP_SECONDS, MAX_BYTES, configured_buttons, lookup_path, names_from_reply

MODES = ("app", "form")
FORM_PROFILE = "d365whsapp-form"  # never d365web's own profile: one Edge process per profile


@dataclass
class Shown:
    summary: dict[str, Any]  # the tool result's page part
    view: View
    line: dict[str, Any]  # the scenario transcript's part for this page
    lines: list[str]  # the step log's lines


def _asks(*texts: Any) -> bool:
    """A page that asks something ("Complete work?"): its only OK answers the question, it does not leave."""
    return any("?" in str(t or "") for t in texts)


class AppBackend:
    mode = "app"
    describe = "Mode app: the mobile app protocol, as the warehouse app on a device."

    def __init__(self, client: WhsClient) -> None:
        self.client = client

    def logoff_notes(self, worker: str) -> list[str]:
        return self.client.logoff_items.notes(worker)

    def limit(self, budget: float) -> None:
        # single calls answer within the budget; a background run's steps keep the full step_timeout
        self.client.call_timeout = max(5.0, min(self.client.config.step_timeout, budget))

    @staticmethod
    def shown(page: Page) -> Shown:
        line: dict[str, Any] = {"page": page.pattern, "at": page.step.get("id", "")}
        if page.message:
            line["message"] = page.message
        return Shown(page.summary(), view_of(page), line, describe_page(page))

    def login(self, worker: str, password: str, company: str | None) -> tuple[Shown, bool]:
        page, resumed = self.client.login(worker, password, company)
        self.client.logoff_items.prefetch(self.client.session(worker).company, worker, self.client._logoff_names)
        return self.shown(page), resumed

    def submit(self, worker: str, values: dict[str, Any] | None, press: str | None,
               in_run: bool = False) -> tuple[Shown, bool]:
        timeout = self.client.config.step_timeout if in_run else None  # a run has no call deadline
        page, recovered = self.client.submit(worker, values, press, timeout)
        return self.shown(page), recovered

    def logoff(self, worker: str) -> Shown | None:
        page = self.client.logoff(worker)
        return None if page is None else self.shown(page)

    def screen(self, worker: str) -> Shown:
        session = self.client.session(worker)
        if session.page is None:
            raise WhsError("no_session", f"worker {worker} has no current page; call whs_login")
        return self.shown(session.page)

    def require_session(self, worker: str) -> None:
        self.client.session(worker)

    def describe_values(self, worker: str, values: dict[str, Any] | None) -> str:
        try:
            page = self.client.session(worker).page
        except WhsError:
            page = None
        return page_values(values, page)

    def error_lines(self, error: WhsError) -> list[str]:
        return describe_page(error.page) if error.page is not None else []


class FormError(WhsError):
    """A d365web refusal or failure, with its details (the form page, if any)."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code, message)
        self.details = details or {}


class FormBackend:
    mode = "form"
    describe = ("Mode form: the in-form WHS app simulator (action:WHSWorkExecute) in the D365 web client, "
                "not the mobile app protocol: context classes may differ, and what the form cannot show "
                "(work-page pattern, a step id without a control named after it, info vs error) is listed in "
                "not_checked, never passed. One browser: calls queue, workers interleave step by step.")

    def __init__(self, web: WebClient, logoff_buttons: object = None) -> None:
        self.web = web
        self.logoff_items = LogoffItems(logoff_buttons)
        self._logoff_worker = ""  # the browser worker serializes walk-outs
        self.lock = threading.Lock()  # one browser: one step at a time, across workers
        self.pages: dict[str, dict[str, Any]] = {}  # worker -> its last page (no browser call to describe)
        self.tabs: dict[str, WebPage] = {}  # worker -> its tab with the form (on the WebClient's worker thread)
        # The idle close would end the logged-in workers' sessions without a walk-out: it waits for them.
        web.keep_open = lambda: bool(self.tabs) and self.tabs_browser is web.browser
        self.tabs_browser: Browser | None = None
        # Accessed on the single browser executor only; fetch itself runs asynchronously in the page.
        self._lookups: dict[str, tuple[WebPage, Future[tuple[str, ...]], float]] = {}
        self._lookup_poll_scheduled = False

    def logoff_notes(self, worker: str) -> list[str]:
        return self.logoff_items.notes(worker)

    def limit(self, budget: float) -> None:
        self.web.budget = max(5.0, budget)

    @staticmethod
    def shown(page: dict[str, Any]) -> Shown:
        line: dict[str, Any] = {"page": page["page"]}
        if page.get("title"):
            line["title"] = page["title"]
        if page.get("message"):
            line["message"] = page["message"]
        return Shown(whsform.public(page), whsform.view_of(page), line, whsform.describe_page(page))

    def _call(self, worker: str, label: str, op: Callable[[], dict[str, Any]], budget: float | None = None,
              in_run: bool = False, collect: bool = False) -> dict[str, Any]:
        """One step (op) on the browser's worker. A run's step waits its turn; a single call waits at most its budget
        (another worker's long step holds the browser) and is then refused as busy."""
        key = worker.strip().upper()
        deadline = time.monotonic() + self.web.budget  # one budget for waiting our turn and for the step
        if not self.lock.acquire(timeout=-1 if in_run else self.web.budget):
            raise WhsError("busy", "the form is busy with another worker's step; try again in a moment")
        left = None if in_run else max(0.1, deadline - time.monotonic())
        if budget is None:
            budget = left
        try:
            try:
                # Only this backend starts runs on its WebClient; each carries the worker as its owner.
                run = self.web.runs.get(BROWSER)
                if run is not None:
                    if not collect or run.owner != key:
                        raise WhsError("busy", f"collect the previous form step with whs_screen for "
                                       f"{run.owner} before starting another operation")
                    page = self.web.wait(left if left is not None else self.web.budget)
                else:
                    page = self.web.call(label, op, budget, owner=key)
            except StillRunning:
                raise WhsError("step_running", f"a form step is still running in D365; call whs_screen for "
                               f"{key} in a few seconds to collect its result or error") from None
            except D365WebError as error:
                raise FormError(error.code, error.message, error.details) from None
            self.pages[key] = page
            return page
        finally:
            self.lock.release()

    # --- the form, one tab per worker (like the app's one session per worker), next to the main tab in
    # the same signed-in browser. These run on WebClient's worker thread (through web.call), with
    # web.page switched to the worker's tab; they use WebClient as the browser library (D38).
    @contextmanager
    def _tab(self, worker: str, create: bool = False) -> Iterator[WebPage]:
        web = self.web
        web._ensure()
        if self.tabs_browser is not web.browser:  # a new browser: the old tabs are gone
            self.tabs, self.tabs_browser = {}, web.browser
        key = worker.strip().upper()
        tab = self.tabs.get(key)
        main = web.page
        if tab is None:
            if not create:
                raise D365WebError("no_session", f"worker {key} is not logged in to the form here; call whs_login")
            # The tab Edge starts with (about:blank) is nothing else's here: the first worker takes it rather
            # than leaving an empty tab next to its own.
            tab = main if main is not None and main not in self.tabs.values() and self._blank(main) \
                else web.browser.new_page()
            self.tabs[key] = tab
        web.page = tab
        try:
            # The worker's tab stays in front afterwards: whoever watches sees the app, not a blank tab.
            try:
                tab.send("Page.bringToFront")
            except BrowserError:
                if web.browser is None or web.browser.conn.closed:
                    raise  # the browser itself: _guarded resets it
                self.tabs.pop(key, None)  # closed by hand or crashed: the session is not here any more
                self.pages.pop(key, None)
                raise D365WebError("no_session", f"worker {key}'s form tab is gone (closed or crashed); "
                                   "call whs_login") from None
            yield tab
        finally:
            web.page = main

    def _blank(self, page: WebPage) -> bool:
        try:
            info = self.web.browser.conn.send("Target.getTargetInfo", {"targetId": page.target_id})["targetInfo"]
        except (BrowserError, KeyError):
            return False
        return info.get("url", "") in ("", "about:blank")

    def _page(self) -> dict[str, Any]:
        raw = self.web._js("whsform")
        if not raw:
            raise D365WebError("not_found", "the WHSWorkExecute form is not open in this worker's tab")
        page = whsform.page_from_form(raw)
        page.update(self.web.browser_mode())
        return page

    def _press(self, name: str) -> None:
        state = self.web._op_press(name, 0)
        if state.get("scope") != "dialog" or state.get("form") != "WHSWorkExecute":  # an infolog box over it
            raise D365WebError("unexpected", f"after {name!r}: {state.get('scope')}:{state.get('form')}",
                               dialog_text=str(state.get("dialog_text", ""))[:500],
                               messages=[m.get("text", "")[:300] for m in state.get("new_messages") or []])

    def _lookup_expression(self, company: str) -> str:
        path = lookup_path(company)
        # Read in this worker's signed-in tab; never pass a bearer token or follow a redirect.
        expression = """(async () => {
            const abort = new AbortController();
            const timer = setTimeout(() => abort.abort(), %d);
            try {
                const reply = await fetch(%s, {method: 'GET', credentials: 'same-origin',
                    redirect: 'error', signal: abort.signal, headers: {Accept: 'application/json'}});
                if (reply.status !== 200) return {status: reply.status, text: ''};
                const reader = reply.body.getReader(), chunks = [];
                let length = 0;
                while (true) {
                    const part = await reader.read();
                    if (part.done) break;
                    length += part.value.length;
                    if (length > %d) {
                        await reader.cancel();
                        return {status: 200, text: '', truncated: true};
                    }
                    chunks.push(part.value);
                }
                const bytes = new Uint8Array(length);
                let offset = 0;
                for (const chunk of chunks) {bytes.set(chunk, offset); offset += chunk.length;}
                return {status: 200, text: new TextDecoder().decode(bytes)};
            } catch (e) {
                // Error classes are useful; arbitrary browser messages can contain URLs or credentials.
                const error = e && e.name === 'AbortError' ? 'AbortError' :
                              e && e.name === 'TypeError' ? 'TypeError' : 'Error';
                return {status: 0, text: '', error};
            }
            finally {clearTimeout(timer);}
        })()""" % (LOOKUP_SECONDS * 1000, json.dumps(path), MAX_BYTES)
        return expression

    def _logoff_names(self, company: str) -> tuple[str, ...]:
        """Synchronous discovery for direct callers; runtime form calls use start/poll below."""
        try:
            reply = self.web.page.evaluate(self._lookup_expression(company),
                                           timeout=LOOKUP_SECONDS + 2, await_promise=True)
        except BrowserError as error:
            raise LookupError(f"page-side OData request failed ({error.code})") from None
        return self._lookup_reply(reply)

    @staticmethod
    def _lookup_reply(reply: Any) -> tuple[str, ...]:
        if not isinstance(reply, dict):
            raise LookupError("page-side OData reply missing")
        if not reply.get("status") and reply.get("error"):
            kind = reply["error"] if reply["error"] in ("AbortError", "TypeError", "Error") else "Error"
            raise LookupError(f"page-side OData request failed ({kind})")
        return names_from_reply(reply.get("status", 0), reply.get("text", ""), reply.get("truncated", False))

    def _start_logoff_names(self, company: str) -> Future[tuple[str, ...]]:
        ident = uuid.uuid4().hex
        expression = """(() => {
            const id = %s;
            const pending = globalThis.__pondWhsLogoffLookups ??= {};
            pending[id] = {done: false};
            (%s).then(reply => {pending[id] = {done: true, reply};});
            return id;
        })()""" % (json.dumps(ident), self._lookup_expression(company))
        page = self.web.page
        result = page.evaluate(expression, timeout=2)
        future: Future[tuple[str, ...]] = Future()
        if result != ident:
            raise LookupError("page-side OData lookup did not start")
        self._lookups[ident] = (page, future, time.monotonic() + LOOKUP_SECONDS + 2)
        self._poll_lookups()
        self._schedule_lookup_poll()
        return future

    def _schedule_lookup_poll(self) -> None:
        if self._lookups and not self._lookup_poll_scheduled:
            self._lookup_poll_scheduled = True
            def enqueue() -> None:
                try:
                    self.web.executor.submit(self._lookup_tick)
                except RuntimeError:
                    pass  # the browser executor has shut down
            timer = threading.Timer(0.25, enqueue)  # a quarter second: one CDP read per pending page
            timer.daemon = True
            timer.start()

    def _lookup_tick(self) -> None:
        self._lookup_poll_scheduled = False
        try:
            self._poll_lookups()
        except Exception:  # noqa: BLE001 -- a tick must never end the chain; the deadline answers the future
            pass
        self._schedule_lookup_poll()

    def _poll_lookups(self) -> None:
        # Polling is short and stays on the browser owner, including while logoff waits.
        for ident, (page, future, deadline) in list(self._lookups.items()):
            expression = """(() => {
                const id = %s, pending = globalThis.__pondWhsLogoffLookups;
                const state = pending && pending[id];
                if (!state) return {missing: true};
                if (state.done || %s) delete pending[id];
                return state;
            })()""" % (json.dumps(ident), "true" if time.monotonic() >= deadline else "false")
            try:
                state = page.evaluate(expression, timeout=2)
                if not isinstance(state, dict) or state.get("missing"):
                    raise LookupError("page-side OData lookup lost (page changed)")
                if state.get("done"):
                    names = self._lookup_reply(state.get("reply"))
                elif time.monotonic() >= deadline:
                    raise LookupError("page-side OData request failed (timeout)")
                else:
                    continue
            except (BrowserError, LookupError) as error:
                self._lookups.pop(ident, None)
                failure = error if isinstance(error, LookupError) else LookupError(
                    f"page-side OData request failed ({error.code})")
                future.set_exception(failure)
            else:
                self._lookups.pop(ident, None)
                future.set_result(names)

    def _worker_logoff_names(self, worker: str, company: str) -> Future[tuple[str, ...]]:
        try:
            with self._tab(worker):
                return self._start_logoff_names(company)
        except (BrowserError, D365WebError) as error:
            raise LookupError(f"page-side OData request failed ({error.code})") from None

    def _company(self) -> str:
        if self.web.page is None:
            return self.web.config.company or ""
        try:
            query = parse_qs(urlsplit(self.web.page.url()).query)
        except BrowserError:
            return self.web.config.company or ""
        return next((v[0] for k, v in query.items() if k.casefold() == "cmp" and v),
                    self.web.config.company or "")

    def _walk_out(self) -> dict[str, Any]:
        items = None
        last = None
        for _ in range(LOGOFF_STEPS_MAX):
            page = self._page()
            if page["page"] == "Login":
                return page
            if items is None:
                try:
                    items = self.logoff_items.get(self._company(), self._logoff_worker, self._start_logoff_names,
                                                 submit=lambda work: work(), pump=self._poll_lookups)
                except LookupPending as pending:  # nothing pressed; the tab and session stay
                    raise D365WebError("retry_later", str(pending), page=whsform.public(page)) from None
            buttons = whsform.view_of(page).buttons
            target = exit_button(buttons, items.names + tuple(self.logoff_items.buttons),
                                 acknowledge=page["page"] == "work" and not page.get("fields")
                                 and not _asks(page.get("title"), (page.get("message") or {}).get("text")))
            if target is None:
                raise D365WebError("stuck", items.stuck(buttons, self.logoff_items.buttons),
                                   page=whsform.public(page))
            seen = (page.get("title"), tuple(page.get("buttons") or ()), tuple(sorted((page.get("fields") or {}).items())), target)
            if seen == last:
                # the same button again on the same page: it does not lead out (it may do work each time)
                raise D365WebError("stuck", f"{target!r} did not leave this page; not pressed again. " +
                                   items.stuck(buttons, self.logoff_items.buttons), page=whsform.public(page))
            last = seen
            self._press(target)
        page = self._page()
        raise D365WebError("stuck", f"still not at the login page after {LOGOFF_STEPS_MAX} presses; " +
                           items.stuck(whsform.view_of(page).buttons, self.logoff_items.buttons),
                           page=whsform.public(page))

    def _close_tab(self, worker: str) -> None:
        web = self.web
        tab = self.tabs.pop(worker.strip().upper(), None)
        if tab is not None and web.browser is not None:
            try:
                if tab is web.page:  # the start tab: emptied, not closed (the last tab would end Edge)
                    tab.send("Page.navigate", {"url": "about:blank"})
                else:
                    web.browser.conn.send("Target.closeTarget", {"targetId": tab.target_id})
            except BrowserError:
                pass

    def _set(self, values: list[tuple[str, Any]]) -> None:
        """A form submit/login may proceed only after every requested value took.
        Keep the setter's comparison rules and diagnostics; mask secret fields at this boundary."""
        try:
            result = self.web._op_set(values)
        except D365WebError as error:
            if "applied" in error.details:
                error.details["applied"] = whsform.public_applied(error.details["applied"])
            raise
        applied = result.get("applied", [])
        # D365's password control keeps what was typed outside its input: the input reads back
        # empty or as a placeholder (live on 10.0.49, 2026-11-05). A secret field cannot be
        # confirmed by readback; the next page confirms it (login_failed, or the form's message).
        if result.get("stopped_at") or len(applied) != len(values) or any(
                item.get("differs") or ("now" not in item and not secret(str(item.get("field", ""))))
                for item in applied):
            raise D365WebError(
                "set_incomplete", "form values were rejected or did not take; no button was pressed",
                applied=whsform.public_applied(applied),
                **{k: result[k] for k in ("stopped_at", "new_messages") if k in result},
                page=whsform.public(self._page()))

    def _op_login(self, worker: str, password: str, company: str | None) -> dict[str, Any]:
        key = worker.strip().upper()
        created = key not in self.tabs
        try:
            return self._login_steps(worker, password, company, resume=not created)
        except D365WebError as error:
            if error.code == "login_failed":
                raise
            if getattr(self, "_login_pressed", False):
                # Login was pressed: the password may have been refused (an infolog), or the login went
                # through. Never type it again on a guess: repeated failures disable the worker.
                raise D365WebError("login_outcome_unknown", f"after pressing Login: {error.message}. Check the "
                                   "worker with whs_screen; do not retry the password before you know it was "
                                   "wrong", **error.details) from None
            if created:
                self._close_tab(worker)  # no session came of it: no tab that claims one
            raise
        finally:
            self._login_pressed = False

    def _login_steps(self, worker: str, password: str, company: str | None, resume: bool = False) -> dict[str, Any]:
        self._login_pressed = False
        with self._tab(worker, create=True):
            if resume and not company and self.web._js("whsform"):
                # The worker's tab holds a session: resume it where it is, as app mode does (Wald,
                # 2026-11-07). Nothing is pressed or typed; whs_logoff ends it.
                page = self._page()
                if page["page"] != "Login":
                    page["resumed"] = True
                    return page
            if company or not self.web._js("whsform"):  # an explicit company always opens the form in it
                self.web._op_open("WHSWorkExecute", company, "action", guard=False)  # the worker's own tab
            self._logoff_worker = worker
            try:
                self._walk_out()
            except D365WebError as error:
                if error.code == "retry_later":  # the tab still shows an earlier session; nothing was pressed
                    raise D365WebError("retry_later", "the worker's tab still shows an earlier session and the "
                                       "logoff menu items are still being looked up; nothing was pressed: call "
                                       "whs_login again in a few seconds", **error.details) from None
                raise
            self._set([("UserId", worker), ("Password", password)])
            self._login_pressed = True
            self._press("Login")
            page = self._page()
            if page["page"] != "Login":
                # Starting the page fetch is brief; completion is polled on this same executor.
                # No collectable user-step result is created for the background lookup.
                self.logoff_items.prefetch(self._company(), worker,
                    lambda company: self._worker_logoff_names(worker, company), self.web.executor.submit)
        if page["page"] == "Login":  # no session: drop the worker's tab again
            self._close_tab(worker)
            raise D365WebError("login_failed", (page.get("message") or {}).get("text")
                               or "login refused (not retried: repeated failures disable the worker)",
                               page=whsform.public(page))
        return page

    def _op_logoff(self, worker: str) -> dict[str, Any]:
        with self._tab(worker):
            self._logoff_worker = worker
            page = self._walk_out()
        self._close_tab(worker)
        return page

    def _op_screen(self, worker: str) -> dict[str, Any]:
        with self._tab(worker):
            return self._page()

    def _op_submit(self, worker: str, values: dict[str, Any] | None, press: str | None) -> dict[str, Any]:
        with self._tab(worker):
            if values:
                self._set([(k, "" if v is None else str(v)) for k, v in values.items()])
            if press:
                self._press(press)
            return self._page()

    # --- the backend ----------------------------------------------------------------------------------

    def login(self, worker: str, password: str, company: str | None) -> tuple[Shown, bool]:
        key, name = worker.strip().upper(), worker.strip()
        if not name:
            raise WhsError("refused", "worker is empty")
        self.pages.pop(key, None)
        try:
            page = self._call(worker, "whs_login", lambda: self._op_login(name, password, company))
        except WhsError:
            self.pages.pop(key, None)
            raise
        resumed = bool(page.pop("resumed", False)) or page["page"] != "Menu"  # a fresh login lands on the main menu
        return self.shown(page), resumed

    def submit(self, worker: str, values: dict[str, Any] | None, press: str | None,
               in_run: bool = False) -> tuple[Shown, bool]:
        budget = self.web.config.step_timeout if in_run else None  # a run waits in its own thread
        page = self._call(worker, "whs_submit", lambda: self._op_submit(worker, values, press), budget, in_run)
        return self.shown(page), False

    def logoff(self, worker: str) -> Shown | None:
        try:
            page = self._call(worker, "whs_logoff", lambda: self._op_logoff(worker))
        except WhsError as error:
            if error.code != "retry_later":  # retry_later keeps the session and its page
                self.pages.pop(worker.strip().upper(), None)
            raise
        self.pages.pop(worker.strip().upper(), None)
        return self.shown(page)

    def screen(self, worker: str) -> Shown:
        return self.shown(self._call(worker, "whs_screen", lambda: self._op_screen(worker), collect=True))

    def require_session(self, worker: str) -> None:
        key = worker.strip().upper()
        if key not in self.tabs:
            raise WhsError("no_session", f"worker {key} is not logged in to the form here; call whs_login")

    def describe_values(self, worker: str, values: dict[str, Any] | None) -> str:
        return whsform.describe_values(values, self.pages.get(worker.strip().upper()))

    def error_lines(self, error: WhsError) -> list[str]:
        page = getattr(error, "details", {}).get("page")
        return [f"  -> {page.get('page', '')} \"{page.get('title', '')}\" (form)"] if isinstance(page, dict) else []


def load_backend(path: Path) -> AppBackend | FormBackend:
    """d365whsapp.json: `mode` app (default; aos_url, tenant, client_id, secret_file, company) or form
    (a `form` block with d365web.json's keys: url, company, profile, debug_port, headless, ...)."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))  # PowerShell 5 Set-Content writes a BOM
    mode = data.get("mode", "app")
    if mode == "app":
        return AppBackend(WhsClient(AppConfig.load(path)))
    if mode != "form":
        raise ValueError(f"{path}: mode must be one of {list(MODES)}: {mode!r}")
    block = data.get("form")
    if not isinstance(block, dict):
        raise ValueError(f"{path}: mode form needs a form block (url, profile, ... as in d365web.json)")
    unknown = sorted(set(block) - {f.name for f in fields(WebConfig)})
    if unknown:
        raise ValueError(f"{path}: form: unknown keys {unknown}")
    config = WebConfig(**{"profile": FORM_PROFILE, **block})
    config.validate()
    return FormBackend(WebClient(config), configured_buttons(data.get("logoff_buttons", [])))
