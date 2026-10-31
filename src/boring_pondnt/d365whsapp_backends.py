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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Callable

from . import d365web_whsform as whsform
from .d365web_browser import Browser, BrowserError, Page as WebPage
from .collect import StillRunning
from .d365web_client import BROWSER, Config as WebConfig, D365WebError, WebClient
from .d365whsapp_client import Config as AppConfig, WhsClient, WhsError, view_of
from .d365whsapp_protocol import Page
from .d365whsapp_steplog import describe_page, page_values
from .whs_steps import View, exit_button

MODES = ("app", "form")
FORM_PROFILE = "d365whsapp-form"  # never d365web's own profile: one Edge process per profile


@dataclass
class Shown:
    summary: dict[str, Any]  # the tool result's page part
    view: View
    line: dict[str, Any]  # the scenario transcript's part for this page
    lines: list[str]  # the step log's lines


class AppBackend:
    mode = "app"
    describe = "Mode app: the mobile app protocol, as the warehouse app on a device."

    def __init__(self, client: WhsClient) -> None:
        self.client = client

    def limit(self, budget: float) -> None:
        self.client.config.step_timeout = max(5.0, min(self.client.config.step_timeout, budget))

    @staticmethod
    def shown(page: Page) -> Shown:
        line: dict[str, Any] = {"page": page.pattern, "at": page.step.get("id", "")}
        if page.message:
            line["message"] = page.message
        return Shown(page.summary(), view_of(page), line, describe_page(page))

    def login(self, worker: str, password: str, company: str | None) -> tuple[Shown, bool]:
        page, resumed = self.client.login(worker, password, company)
        return self.shown(page), resumed

    def submit(self, worker: str, values: dict[str, Any] | None, press: str | None,
               in_run: bool = False) -> tuple[Shown, bool]:
        page, recovered = self.client.submit(worker, values, press)
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

    def __init__(self, web: WebClient) -> None:
        self.web = web
        self.lock = threading.Lock()  # one browser: one step at a time, across workers
        self.pages: dict[str, dict[str, Any]] = {}  # worker -> its last page (no browser call to describe)
        self.tabs: dict[str, WebPage] = {}  # worker -> its tab with the form (on the WebClient's worker thread)
        self.tabs_browser: Browser | None = None

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
        if not self.lock.acquire(timeout=-1 if in_run else self.web.budget):
            raise WhsError("busy", "the form is busy with another worker's step; try again in a moment")
        try:
            try:
                # Only this backend starts runs on its WebClient; each carries the worker as its owner.
                run = self.web.runs.get(BROWSER)
                if run is not None:
                    if not collect or run.owner != key:
                        raise WhsError("busy", f"collect the previous form step with whs_screen for "
                                       f"{run.owner} before starting another operation")
                    page = self.web.wait(self.web.budget)
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
            # than leaving an empty tab next to its own (Wald, watching, 2026-10-27).
            tab = main if main is not None and main not in self.tabs.values() and self._blank(main) \
                else web.browser.new_page()
            self.tabs[key] = tab
        web.page = tab
        try:
            # The worker's tab stays in front afterwards: whoever watches sees the app, not a blank tab.
            tab.send("Page.bringToFront")
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
        return whsform.page_from_form(raw)

    def _press(self, name: str) -> None:
        state = self.web._op_press(name, 0)
        if state.get("scope") != "dialog" or state.get("form") != "WHSWorkExecute":  # an infolog box over it
            raise D365WebError("unexpected", f"after {name!r}: {state.get('scope')}:{state.get('form')}",
                               dialog_text=str(state.get("dialog_text", ""))[:500],
                               messages=[m.get("text", "")[:300] for m in state.get("new_messages") or []])

    def _walk_out(self) -> dict[str, Any]:
        for _ in range(12):
            page = self._page()
            if page["page"] == "Login":
                return page
            target = exit_button(whsform.view_of(page).buttons)
            if target is None:
                raise D365WebError("stuck", "no Log off, Cancel, Back or Leave on this page", page=whsform.public(page))
            self._press(target)
        raise D365WebError("stuck", "still not at the login page after 12 presses",
                           page=whsform.public(self._page()))

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
        if result.get("stopped_at") or len(applied) != len(values) or any(
                item.get("differs") or "now" not in item for item in applied):
            raise D365WebError(
                "set_incomplete", "form values were rejected or did not take; no button was pressed",
                applied=whsform.public_applied(applied),
                **{k: result[k] for k in ("stopped_at", "new_messages") if k in result},
                page=whsform.public(self._page()))

    def _op_login(self, worker: str, password: str, company: str | None) -> dict[str, Any]:
        with self._tab(worker, create=True):
            if company or not self.web._js("whsform"):  # an explicit company always opens the form in it
                self.web._op_open("WHSWorkExecute", company, "action")
            self._walk_out()
            self._set([("UserId", worker), ("Password", password)])
            self._press("Login")
            page = self._page()
        if page["page"] == "Login":  # no session: drop the worker's tab again
            self._close_tab(worker)
            raise D365WebError("login_failed", (page.get("message") or {}).get("text")
                               or "login refused (not retried: repeated failures disable the worker)",
                               page=whsform.public(page))
        return page

    def _op_logoff(self, worker: str) -> dict[str, Any]:
        with self._tab(worker):
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
        return self.shown(page), page["page"] != "Menu"  # a fresh login lands on the main menu

    def submit(self, worker: str, values: dict[str, Any] | None, press: str | None,
               in_run: bool = False) -> tuple[Shown, bool]:
        budget = self.web.config.step_timeout if in_run else None  # a run waits in its own thread
        page = self._call(worker, "whs_submit", lambda: self._op_submit(worker, values, press), budget, in_run)
        return self.shown(page), False

    def logoff(self, worker: str) -> Shown | None:
        try:
            page = self._call(worker, "whs_logoff", lambda: self._op_logoff(worker))
        finally:
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
    return FormBackend(WebClient(config))
