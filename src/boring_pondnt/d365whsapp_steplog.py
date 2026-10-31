"""A readable log of what each WHS worker did and saw, one plain-text file per worker.

Every whs_login, whs_submit, whs_logoff and scenario step appends one entry, in both
modes (app and form, D38): the
local time, the worker, the action (the values set, the button pressed), then the
page D365 answered with: page type, title, step, each control as
`label [name]: value`, the buttons and the message. It records what the app
"saw", not how the Microsoft app draws it. Passwords never reach the log: the
login's is not written at all, and a value for a password control is masked.

Files live in STATE/whs-steps/steps-<WORKER>.log; past MAX_BYTES the file moves
to .log.1 (one old file kept). A failed write never fails the step: the caller
reports it once as `log_error`.
"""
from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .d365whsapp_protocol import MESSAGE_CONTROL, Page

MAX_BYTES = 1024 * 1024
TAIL_BYTES = 64 * 1024
MASK = "***"
_SECRET_NAME = re.compile(r"password|passwd|secret|pin\b", re.IGNORECASE)
_UNSAFE = re.compile(r"[^A-Z0-9_.-]")


def _file_name(worker: str) -> str:
    name = _UNSAFE.sub("_", worker.upper()) or "_"
    return f"steps-{name}.log"


def secret(name: str, password_control: bool = False) -> bool:
    """The one masking rule, for the log and for form results: a password control, or a name
    that says password, secret or PIN."""
    return password_control or bool(_SECRET_NAME.search(name))


def masked(name: str, value: Any, password_control: bool = False) -> Any:
    return MASK if value and secret(name, password_control) else value


@dataclass
class Entry:
    """One control as the log shows it; both page formats (protocol, form) map to this."""
    name: str
    label: str = ""
    value: Any = ""
    password: bool = False
    readonly: bool = False
    error: bool = False


def _named(entry_label: str, name: str) -> str:
    label = entry_label.strip()
    return f"{label} [{name}]" if label and label != name else f"[{name}]"


def _one_line(text: str) -> str:
    """Multi-line page text, indented under its entry (keeps the log's shape)."""
    return text.strip().replace("\r\n", "\n").replace("\n", "\n       ")


def describe_values(values: dict[str, Any] | None, labels: dict[str, str],
                    passwords: Iterable[str] = ()) -> str:
    """{label [name]: value} for the fields set, against the page they were set on."""
    if not values:
        return ""
    hidden = set(passwords)
    parts = [f"{_named(labels.get(name, ''), name)}: {MASK if secret(name, name in hidden) else value}"
             for name, value in values.items()]
    return "{" + ", ".join(parts) + "}"


def describe(head: str, entries: Iterable[Entry], buttons: list[str], message: str) -> list[str]:
    """The log's lines for one page: head, each control, the buttons, the message."""
    lines = [f"  -> {head}"]
    for entry in entries:
        value = masked(entry.name, entry.value, entry.password)
        text = _named(entry.label, entry.name) + (f": {value}" if value else ":")
        if entry.readonly:
            text += " (read-only)"
        if entry.error:
            text += " (error)"
        lines.append("     " + _one_line(text))
    if buttons:
        lines.append("     buttons: " + ", ".join(buttons))
    if message:
        lines.append(f"     {_one_line(message)}")
    return lines


def page_values(values: dict[str, Any] | None, page: Page | None) -> str:
    """describe_values against a protocol page."""
    controls = page.controls if page is not None else []
    return describe_values(values, {c.name: c.label or "" for c in controls},
                           [c.name for c in controls if c.type == "password"])


def describe_page(page: Page) -> list[str]:
    """The log's lines for a protocol page."""
    head = page.pattern
    title = str(page.summary().get("title") or "").strip()
    if title:
        head += f' "{title}"'
    if page.step:
        head += f' step {page.step.get("id", "")}'
        if page.step.get("title"):
            head += f' "{page.step["title"]}"'
    buttons = page.buttons()
    entries = [Entry(c.name, c.label or "", c.selected or c.data, c.type == "password", not c.enabled, c.error)
               for c in page.controls
               if c.name != MESSAGE_CONTROL and c.name not in buttons
               and not (page.pattern == "Menu" and c.type == "label")]
    message = f"message {page.message.get('kind', '')}: {page.message.get('text', '')}" if page.message else ""
    return describe(head, entries, buttons, message)


class StepLog:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.lock = threading.Lock()
        self.failed: set[str] = set()  # workers whose last write failed (reported once)

    def path(self, worker: str) -> Path:
        return self.directory / _file_name(worker)

    def record(self, worker: str, action: str, page: Page | None = None, *, error: str = "",
               recovered: bool = False, lines: list[str] | None = None) -> str | None:
        """Append one entry; returns a log_error text the first time a write fails, else None.
        `lines`: the page already described (form mode: d365web_whsform.describe_page)."""
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = [f"{stamp}  {worker.upper()}  {action}"]
        if error:
            entry.append(f"  -> error {error}")
        if page is not None:
            entry += describe_page(page)
        entry += lines or []
        if recovered:
            entry.append("     recovered: true (the reply was lost; the session was resumed, check the page)")
        text = "\n".join(entry) + "\n\n"
        key = worker.upper()
        try:
            with self.lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                target = self.path(worker)
                if target.exists() and target.stat().st_size + len(text.encode()) > MAX_BYTES:
                    target.replace(target.with_name(target.name + ".1"))
                with open(target, "a", encoding="utf-8", newline="\n") as stream:
                    stream.write(text)
            self.failed.discard(key)
            return None
        except OSError as exc:
            if key in self.failed:
                return None
            self.failed.add(key)
            return f"step log not written ({type(exc).__name__}: {exc}); the step itself ran"

    def tail(self, worker: str, lines: int) -> str | None:
        """The last `lines` lines (at most TAIL_BYTES), or None when the worker has no log."""
        target = self.path(worker)
        try:
            with open(target, "rb") as stream:
                stream.seek(0, 2)
                size = stream.tell()
                stream.seek(max(0, size - TAIL_BYTES))
                data = stream.read().decode("utf-8", errors="replace")
        except FileNotFoundError:
            return None
        rows = data.rstrip("\n").splitlines()  # entries end with a blank line
        if size > TAIL_BYTES and rows:
            rows = rows[1:]  # the first row may be cut
        return "\n".join(rows[-lines:])
