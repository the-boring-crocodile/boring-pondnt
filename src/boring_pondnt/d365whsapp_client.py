"""d365whsapp client: Entra token, the mobile app service, one session per worker.

One worker has one server session (WHSWorkUserSession is keyed by user), so a
test run uses its own worker. Each worker gets a stable device id derived from
its name: a second device would take the session over.

Recovery (P0): when a reply is lost (timeout) or the server rejects the token,
the step may or may not have run. Never resend blindly: log in again, and the
server resumes the session at its last processed page. The result says so.
Failed logins are never retried: repeated failures disable the worker.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .d365whsapp_protocol import BUTTON_TYPES, Page, ProtocolError, ServiceError, build_request, parse_page, parse_service_error
from .whs_steps import View, exit_button

SERVICE_PATH = "/api/services/WHSMobileAppServices/WHSMobileAppService"
STEP_TIMEOUT_DEFAULT = 300.0  # a confirm with work creation took > 28 s live
TOKEN_TIMEOUT = 30.0
DEVICE_NAMESPACE = uuid.UUID("6f1c1d0e-3a52-4b8e-9f0b-776873617070")
LOGOFF_STEPS_MAX = 8


class WhsError(Exception):
    """A failure the tool reports. `code`: a stable reason; `page`: the page, if any."""

    def __init__(self, code: str, message: str, page: Page | None = None) -> None:
        super().__init__(message)
        self.code, self.message, self.page = code, message, page


SESSION_BUSY = "ERROR_SESSION_BUSY"  # the server still runs an earlier request of this worker


def step_running(seconds: float | None = None) -> WhsError:
    waited = f"no reply within {seconds:.0f} s and " if seconds else ""
    return WhsError("step_running",
                    f"{waited}a step of this worker is still running in D365; call whs_login for this worker "
                    "again in a few seconds: the session resumes at the step's result")


@dataclass
class Config:
    aos_url: str
    tenant: str
    client_id: str
    secret_file: str = ""
    company: str = ""  # no built-in company: whs_login(company=...) or this default
    step_timeout: float = STEP_TIMEOUT_DEFAULT

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = json.loads(path.read_text(encoding="utf-8-sig"))  # PowerShell 5 Set-Content writes a BOM
        known = {k: data[k] for k in ("aos_url", "tenant", "client_id", "secret_file", "company", "step_timeout")
                 if k in data}
        return cls(**known)


def read_dpapi_secret(path: str) -> str:
    """A secret written by `ConvertFrom-SecureString` (DPAPI, current user): hex of the blob."""
    if sys.platform != "win32":
        raise WhsError("secret_unavailable", "DPAPI secrets can only be read on Windows")
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    try:
        raw = bytes.fromhex(Path(path).read_text(encoding="utf-8-sig").strip())
    except (OSError, ValueError) as exc:
        raise WhsError("secret_unavailable", f"cannot read the client secret file: {exc}") from None
    buffer = ctypes.create_string_buffer(raw, len(raw))
    blob_in, blob_out = Blob(len(raw), buffer), Blob()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32  # type: ignore[attr-defined]
    if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise WhsError("secret_unavailable", "DPAPI could not decrypt the client secret (other user or machine?)")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData).decode("utf-16-le")
    finally:
        kernel32.LocalFree(blob_out.pbData)


@dataclass
class Response:
    status: int
    text: str


Transport = Callable[[str, dict[str, str], bytes, float], Response]


def urllib_transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> Response:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return Response(response.status, response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return Response(exc.code, exc.read().decode("utf-8", "replace"))


class TokenSource:
    def __init__(self, config: Config, transport: Transport, secret: Callable[[], str]) -> None:
        self.config, self.transport, self.secret = config, transport, secret
        self._token, self._expires = "", 0.0
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._expires - 120:
                return self._token
            body = urllib.parse.urlencode({
                "grant_type": "client_credentials", "client_id": self.config.client_id,
                "client_secret": self.secret(), "scope": f"{self.config.client_id}/.default"}).encode()
            url = f"https://login.microsoftonline.com/{self.config.tenant}/oauth2/v2.0/token"
            try:
                response = self.transport(url, {"Content-Type": "application/x-www-form-urlencoded"}, body, TOKEN_TIMEOUT)
            except (OSError, socket.timeout) as exc:
                raise WhsError("token_failed", f"Entra token request failed: {exc}") from None
            if response.status != 200:
                raise WhsError("token_failed", f"Entra token request: HTTP {response.status}: {response.text[:300]}")
            data = json.loads(response.text)
            self._token = data["access_token"]
            self._expires = time.monotonic() + float(data.get("expires_in", 3600))
            return self._token


def device_id_for(worker: str) -> str:
    return "{" + str(uuid.uuid5(DEVICE_NAMESPACE, f"d365whsapp:{worker.upper()}")).upper() + "}"


@dataclass
class WorkerSession:
    worker: str
    company: str
    device_id: str
    password: str = field(repr=False, default="")
    page: Page | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


class WhsClient:
    def __init__(self, config: Config, transport: Transport = urllib_transport,
                 secret: Callable[[], str] | None = None) -> None:
        self.config, self.transport = config, transport
        self.tokens = TokenSource(config, transport, secret or (lambda: read_dpapi_secret(config.secret_file)))
        self.sessions: dict[str, WorkerSession] = {}
        self._lock = threading.Lock()

    # --- the service -------------------------------------------------------
    def call(self, method: str, payload: dict[str, Any], timeout: float | None = None) -> str:
        url = self.config.aos_url.rstrip("/") + f"{SERVICE_PATH}/{method}"
        headers = {"Authorization": f"Bearer {self.tokens.get()}", "Content-Type": "application/json"}
        try:
            response = self.transport(url, headers, json.dumps(payload).encode(), timeout or self.config.step_timeout)
        except (socket.timeout, TimeoutError) as exc:
            raise WhsError("timeout", f"no reply within {timeout or self.config.step_timeout:.0f} s: {exc}") from None
        except OSError as exc:
            raise WhsError("unreachable", f"AOS not reachable: {exc}") from None
        if response.status != 200:
            error = parse_service_error(response.status, response.text)
            raise WhsError(error.code or f"http_{response.status}", error.message)
        try:
            value = json.loads(response.text)
        except ValueError:
            raise WhsError("bad_reply", f"reply is not JSON: {response.text[:200]}") from None
        return value

    def exchange(self, xml: str, company: str) -> Page:
        reply = self.call("getNextFormHandHeld", {"_xml": xml, "_dataAreaId": company})
        if not isinstance(reply, str):
            raise WhsError("bad_reply", "getNextFormHandHeld did not return a string")
        try:
            return parse_page(reply)
        except ProtocolError as exc:
            raise WhsError("bad_reply", str(exc)) from None

    def companies(self) -> list[dict[str, str]]:
        reply = self.call("getCompanies", {}, timeout=60)
        return [{"id": c.get("dataArea", ""), "name": c.get("name", "")} for c in reply or []]

    # --- sessions ----------------------------------------------------------
    def session(self, worker: str) -> WorkerSession:
        with self._lock:
            found = self.sessions.get(worker.upper())
        if found is None:
            raise WhsError("no_session", f"worker {worker} is not logged in here; call whs_login first")
        return found

    def login(self, worker: str, password: str, company: str | None = None) -> tuple[Page, bool]:
        """Log in; returns the page and whether the server resumed an existing session."""
        company = (company or self.config.company).strip()
        if not company:
            raise WhsError("no_company", "no company: pass company to whs_login or set \"company\" in d365whsapp.json")
        key = worker.upper()
        with self._lock:
            session = self.sessions.get(key)
            if session is None or session.company != company:
                session = WorkerSession(worker=worker, company=company, device_id=device_id_for(worker))
                self.sessions[key] = session
        with session.lock:
            page = self._login(session, password)
            session.password = password
            return page, not page.is_login and page.pattern != "Menu"

    def _login(self, session: WorkerSession, password: str) -> Page:
        try:
            return self._login_once(session, password)
        except WhsError as exc:
            if exc.code == SESSION_BUSY:
                raise step_running() from None
            raise

    def _login_once(self, session: WorkerSession, password: str) -> Page:
        login_page = self.exchange("", session.company)
        if not login_page.is_login:
            raise WhsError("unexpected_page", f"expected the login page, got {login_page.pattern}", login_page)
        request = build_request(login_page, {"UserId": session.worker, "Password": password}, "Login",
                                device_id=session.device_id)
        page = self.exchange(request, session.company)
        if page.is_login:
            session.page = None
            text = page.message["text"] if page.message else "login refused"
            raise WhsError("login_failed", f"{text} (not retried: repeated failures disable the worker)", page)
        session.page = page
        return page

    def submit(self, worker: str, values: dict[str, str] | None, press: str | None) -> tuple[Page, bool]:
        """One step. Returns the next page and whether it came from a recovery login."""
        session = self.session(worker)
        with session.lock:
            if session.page is None:
                raise WhsError("no_session", f"worker {worker} has no current page; call whs_login")
            try:
                request = build_request(session.page, values, press, device_id=session.device_id)
            except ProtocolError as exc:
                raise WhsError("refused", str(exc), session.page) from None
            try:
                page = self.exchange(request, session.company)
            except WhsError as exc:
                if exc.code == "REQUEST_XML_TAMPERING":
                    raise  # the session is intact; the current page still holds
                if exc.code == SESSION_BUSY:
                    raise step_running() from None
                if exc.code in ("timeout", "REQUEST_SESSION_TOKEN_ERROR", "MULTI_DEVICE_USER_LOGIN") and session.password:
                    try:
                        page = self._login(session, session.password)
                    except WhsError as again:
                        if again.code == "step_running" and exc.code == "timeout":
                            raise step_running(self.config.step_timeout) from None
                        raise
                    return page, True
                raise
            session.page = page
            if page.is_login:  # the server ended the session (log off, or an unreadable request)
                with self._lock:
                    self.sessions.pop(worker.upper(), None)
            return page, False

    def logoff(self, worker: str) -> Page | None:
        """Cancel/Back to the main menu and press Log off. Ends the local session either way."""
        session = self.session(worker)
        page = session.page
        try:
            for _ in range(LOGOFF_STEPS_MAX):
                if page is None or page.is_login:
                    return page
                press = exit_button(view_of(page).buttons)
                if press is None:
                    raise WhsError("stuck", f"no Log off, Cancel or Back on page {page.pattern}; the server "
                                   "session stays there and resumes at the next whs_login", page)
                page, _ = self.submit(worker, None, press)
            raise WhsError("stuck", f"not logged off after {LOGOFF_STEPS_MAX} steps", page)
        finally:
            with self._lock:
                self.sessions.pop(worker.upper(), None)


def view_of(page: Page) -> View:
    """The protocol page as whs_steps sees it: everything is visible."""
    return View(
        page=page.pattern,
        step=page.step.get("id", ""),
        title=str(page.summary().get("title", "")),
        message_text=(page.message or {}).get("text", ""),
        message_kind=(page.message or {}).get("kind", ""),
        fields={c.name: c.data for c in page.controls},
        labels={c.name: c.label for c in page.controls},
        buttons=[(c.name, c.label) for c in page.controls if c.enabled and c.type in BUTTON_TYPES],
    )
