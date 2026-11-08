"""Discover warehouse logoff names through the standard, company-scoped public entity."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from urllib.parse import urlencode

ENTITY = "WarehouseMobileDeviceMenuItemsV4"
MAX_BYTES = 256 * 1024
# The lookup itself may take this long in the background: right after a cold start the first OData
# call took longer than the old 30 s bound (a test box, 2026-11-05: AbortError at 8 s, then logoff stuck).
LOOKUP_SECONDS = 120
# A logoff waits this long for the names, then answers retry_later and keeps the session; the lookup
# goes on in the background and the next whs_logoff usually finds its answer cached (Wald, 2026-11-05:
# "return the issue and the model retries with a grace period").
FOREGROUND_SECONDS = 8.0
# A failed lookup is retried after this long. Live on a dev box (2026-11-05) the first lookup right
# after a restart answered HTTP 400 once; cached for the backend's life, logoff then stayed
# stuck until the next restart. Successes and empty replies stay cached.
RETRY_FAILED_SECONDS = 60.0
STALE_MARGIN = 30.0  # seconds past LOOKUP_SECONDS before an unanswered read counts as lost
FALLBACK_LABELS = ["Log off", "Cancel / Cancel…", "Leave…", "Done", "Back"]


class LookupError(ValueError):
    """A bounded discovery failure; its message is safe for the step log."""


class LookupPending(Exception):
    """The names are not known yet; the lookup is still running. Nothing was pressed."""

    def __init__(self, waited: float) -> None:
        super().__init__(
            f"logoff retry_later: the logoff menu item lookup is still running after {waited:.0f} s "
            f"(it may take up to {LOOKUP_SECONDS} s right after a D365 start); nothing was pressed and "
            "the session is kept; call whs_logoff again in a few seconds")


def configured_buttons(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(n, str) or not n.strip() for n in value):
        raise ValueError("logoff_buttons must be a list of nonempty button names or labels")
    return [n.strip() for n in value]


def lookup_path(company: str) -> str:
    if not company.strip():
        raise LookupError("company unavailable")
    # WHSWorkActivity is the enum verified in PLAN_WHS_LOGOFF_BY_ACTIVITY_2026-11-05.
    # Use the qualified OData enum literal, not a string literal or a translated label.
    company_literal = company.strip().replace("'", "''")
    where = ("ActivityCode eq Microsoft.Dynamics.DataEntities.WHSWorkActivity'LogOff'"
             f" and dataAreaId eq '{company_literal}'")
    return "/data/" + ENTITY + "?" + urlencode({
        "cross-company": "true", "$filter": where, "$select": "MenuItemName,Title",
    })


def names_from_reply(status: int, text: str, truncated: bool = False) -> tuple[str, ...]:
    if status != 200:
        raise LookupError(f"HTTP {status}" if status else "request failed or timed out")
    if truncated or len(text.encode("utf-8")) > MAX_BYTES:
        raise LookupError("reply exceeded the lookup byte limit")
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        raise LookupError("reply was not JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("value"), list):
        raise LookupError("reply had no entity rows")
    if data.get("@odata.nextLink") or len(data["value"]) > 128:
        raise LookupError("entity rows exceeded the lookup page limit")
    names = []
    for row in data["value"]:
        if not isinstance(row, dict):
            raise LookupError("invalid entity row")
        for field in ("MenuItemName", "Title"):
            name = row.get(field)
            if name is None:
                continue
            if not isinstance(name, str) or len(name) > 512:
                raise LookupError("invalid menu item name or title")
            if name.strip() and name.strip() not in names:
                names.append(name.strip())
    return tuple(names)


@dataclass(frozen=True)
class Items:
    names: tuple[str, ...] = ()
    failure: str = ""

    def stuck(self, buttons: list[tuple[str, str]], configured: list[str]) -> str:
        return (f"logoff: ActivityCode LogOff names/titles {list(self.names)!r}; "
                f"lookup {self.failure or 'succeeded'}; configured logoff_buttons {configured!r}; "
                f"English labels tried {FALLBACK_LABELS!r}; page buttons (name, label) {buttons!r}")


@dataclass
class Lookup:
    origin: str
    company: str
    worker: str
    started: float
    future: Future[Items]
    item: Items | None = None
    finished: float = 0.0


class LogoffItems:
    """Cache good replies permanently; logoff failures throttle fresh reads for 60 seconds."""

    def __init__(self, buttons: object = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.buttons = configured_buttons([] if buttons is None else buttons)
        self._clock = clock
        self._cache: dict[str, Items] = {}
        self._login: dict[str, Lookup] = {}
        self._logoff: dict[str, Lookup] = {}
        self._notes: dict[str, list[str]] = {}
        self._observer: Callable[[str, str], None] | None = None
        self._changed = threading.Condition()
        self._notes_lock = threading.Lock()

    def observe(self, observer: Callable[[str, str], None]) -> None:
        """Record completed lookups immediately, including background lookups."""
        with self._notes_lock:
            self._observer = observer

    def _new(self, origin: str, company: str, worker: str) -> Lookup:
        return Lookup(origin, company, worker, self._clock(), Future())

    def _stale(self, attempt: Lookup | None) -> bool:
        """An unanswered read past every reader's deadline: never waited for again (a lost future
        would otherwise answer retry_later for good); a fresh read starts instead."""
        return attempt is not None and attempt.item is None and \
            self._clock() - attempt.started > LOOKUP_SECONDS + STALE_MARGIN

    def get(self, company: str, worker: str,
            read: Callable[[str], tuple[str, ...] | Future[tuple[str, ...]]],
            submit: Callable[[Callable[[], None]], object] | None = None,
            pump: Callable[[], None] | None = None, wait: float | None = None) -> Items:
        """One fresh logoff read races login's read; a failed login never imposes the cooldown.
        Raises LookupPending when no answer came within `wait` (default FOREGROUND_SECONDS); the reads go on."""
        wait = FOREGROUND_SECONDS if wait is None else wait
        key = company.strip().casefold()
        started = time.monotonic()
        with self._changed:
            if key in self._cache:
                return self._cache[key]
            own = self._logoff.get(key)
            if self._stale(own):
                own = None
            if own is not None and own.item is not None:
                if self._clock() - own.finished < RETRY_FAILED_SECONDS:
                    return own.item
                own = None
            owner = own is None
            if own is None:
                own = self._new("logoff", company, worker)
                self._logoff[key] = own
            login = self._login.get(key)
            if self._stale(login):
                login = None
        if owner:
            self._launch(key, own, read, submit)
        while True:
            # Form mode pumps short page-result reads on the browser owner. No second
            # browser thread, and neither request occupies the executor while fetching.
            if pump is not None:
                pump()
            with self._changed:
                if key in self._cache:
                    return self._cache[key]
                if own.item is not None and (login is None or login.item is not None):
                    return own.item
                waited = time.monotonic() - started
                if waited >= wait:
                    if own.item is not None:
                        return own.item  # our own answer (a failure) is in; login's read is still out
                    raise LookupPending(waited)
                self._changed.wait(0.05)

    def prefetch(self, company: str, worker: str,
                 read: Callable[[str], tuple[str, ...] | Future[tuple[str, ...]]],
                 submit: Callable[[Callable[[], None]], object] | None = None) -> Future[Items]:
        """Start once per company without waiting; browser reads may return a future."""
        key = company.strip().casefold()
        with self._changed:
            if key in self._cache:
                done: Future[Items] = Future()
                done.set_result(self._cache[key])
                return done
            for attempt in (self._logoff.get(key), self._login.get(key)):
                if attempt is not None and not self._stale(attempt) and (attempt.item is None or
                                           self._clock() - attempt.finished < RETRY_FAILED_SECONDS):
                    return attempt.future
            attempt = self._new("login", company, worker)
            self._login[key] = attempt
        self._launch(key, attempt, read, submit)
        return attempt.future

    def _launch(self, key: str, attempt: Lookup,
                read: Callable[[str], tuple[str, ...] | Future[tuple[str, ...]]],
                submit: Callable[[Callable[[], None]], object] | None) -> None:
        work = lambda: self._lookup(key, attempt, read)
        try:
            if submit is None:
                threading.Thread(target=work, name="whs-logoff-lookup", daemon=True).start()
            else:
                submit(work)
        except RuntimeError:
            self._finish(key, attempt, Items(failure="background lookup could not start"))

    def _lookup(self, key: str, attempt: Lookup,
                read: Callable[[str], tuple[str, ...] | Future[tuple[str, ...]]]) -> None:
        attempt.started = self._clock()
        def answer(result: Future[tuple[str, ...]]) -> None:
            try:
                names = result.result()
                item = Items(names, "" if names else "no LogOff items returned")
            except LookupError as error:
                item = Items(failure=safe_lookup_text(str(error)))
            except Exception as error:
                item = Items(failure=f"lookup failed ({type(error).__name__})")
            self._finish(key, attempt, item)
        try:
            result = read(attempt.company)
        except Exception as error:
            failed: Future[tuple[str, ...]] = Future()
            failed.set_exception(error)
            answer(failed)
        else:
            if isinstance(result, Future):
                result.add_done_callback(answer)
            else:
                ready: Future[tuple[str, ...]] = Future()
                ready.set_result(result)
                answer(ready)

    def _finish(self, key: str, attempt: Lookup, item: Items) -> None:
        finished = self._clock()
        elapsed_ms = max(0, round((finished - attempt.started) * 1000))
        outcome = ("error " + item.failure if item.failure else "names/titles " + repr(list(item.names)))
        message = safe_lookup_text(
            f"logoff lookup {ENTITY}, source {attempt.origin}, company {attempt.company!r}: "
            f"{elapsed_ms} ms; {outcome}")
        with self._notes_lock:
            observer = self._observer
            if observer is None:
                self._notes.setdefault(attempt.worker.strip().upper(), []).append(message)
        if observer is not None:
            try:
                observer(attempt.worker, message)
            except Exception as error:
                logging.getLogger(__name__).warning("logoff lookup log failed (%s)", type(error).__name__)
        with self._changed:
            attempt.item, attempt.finished = item, finished
            if not item.failure or item.failure == "no LogOff items returned":
                # A late failure or second success cannot replace the first good answer.
                self._cache.setdefault(key, item)
            self._changed.notify_all()
        attempt.future.set_result(item)

    def notes(self, worker: str) -> list[str]:
        with self._notes_lock:
            return self._notes.pop(worker.strip().upper(), [])


def safe_lookup_text(value: str) -> str:
    """Lookup diagnostics contain outcomes, never request URLs or credential values."""
    value = re.sub(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s'\",<>]+", "[URL]", value)
    value = re.sub(r"(?i)\bBearer\s+[^\s'\",<>]+|\bbsk_[a-z0-9_-]+", "[credential]", value)
    return re.sub(r"(?i)\b(token|secret|password)\s*[:=]\s*[^\s'\",<>]+",
                  r"\1=[credential]", value)
