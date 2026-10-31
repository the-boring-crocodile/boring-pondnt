"""WHS app steps and their expectations, for both d365whsapp backends (D38).

The one implementation of: the step format (`values`, `press`, `expect`, `note`), the expectation
check and the way out of a page (logoff). Each backend reads its page into a `View`: the mobile app
protocol (`d365whsapp_client.view_of`, sees everything) or the in-form simulator
(`d365web_whsform.view_of`, cannot see the pattern of work pages, a step id without a control named
after it, or whether a message is an error or an info). What a view cannot see is reported as not
checked, never as passed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .scenario_values import check_scalar_map

STEP_KEYS = {"values", "press", "expect", "note"}
EXPECT_KEYS = {"page", "step", "title", "message_kind", "message", "fields", "labels"}


class StepError(ValueError):
    """A step list the runner refuses before running anything."""


@dataclass
class View:
    """What a backend can tell about one page. None: this backend cannot see it."""
    page: str | None  # Login, Menu or the protocol pattern; None: a work page in the form
    step: str | None  # the Process Guide step id ("" when none); None: not shown (see step_names)
    title: str = ""
    message_text: str = ""
    message_kind: str | None = ""  # None: there is a message but its kind is not visible
    fields: dict[str, Any] = field(default_factory=dict)  # name -> value
    labels: dict[str, str] = field(default_factory=dict)  # name -> label text
    buttons: list[tuple[str, str]] = field(default_factory=list)  # enabled buttons: (name, label)
    step_names: list[str] = field(default_factory=list)  # control names, to find a step when step is None


def _text(value: Any, name: str) -> None:
    if not isinstance(value, str):
        raise StepError(f"{name}: expected text")


def _values(value: Any, name: str) -> None:
    check_scalar_map(value, name, StepError)


def check_steps(steps: Any) -> None:
    if not isinstance(steps, list) or not steps:
        raise StepError("steps is empty")
    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise StepError(f"step {index}: an object with values/press/expect/note")
        unknown = set(step) - STEP_KEYS
        if unknown:
            raise StepError(f"step {index}: unknown keys {sorted(unknown)}")
        try:
            for name in ("press", "note"):
                if name in step and not (name == "press" and step[name] is None):
                    _text(step[name], name)
            if step.get("values") is not None:
                _values(step["values"], "values")
            expect = step.get("expect", {})
            if not isinstance(expect, dict):
                raise StepError("expect must be an object")
            unknown = set(expect) - EXPECT_KEYS
            if unknown:
                raise StepError(f"unknown expectations {sorted(unknown)}")
            for name in EXPECT_KEYS - {"fields", "labels"}:
                if name in expect:
                    _text(expect[name], f"expect.{name}")
            for name in ("fields", "labels"):
                if name in expect:
                    _values(expect[name], f"expect.{name}")
        except StepError as error:
            raise StepError(f"step {index}: {error}") from None



def check(view: View, expect: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(misses, expectations this view cannot show)."""
    misses: list[str] = []
    unseen: list[str] = []

    def differ(key: str, want: Any, got: Any) -> None:
        if want != got:
            misses.append(f"{key}: expected {want!r}, got {got!r}")

    if "page" in expect:
        if view.page is not None:
            differ("page", expect["page"], view.page)
        elif expect["page"] in ("Login", "Menu"):
            misses.append(f"page: expected {expect['page']!r}, got a work page")
        else:
            unseen.append("page (pattern of work pages)")
    if "step" in expect:
        want = str(expect["step"])
        if view.step is not None:
            differ("step", want, view.step)
        elif want not in view.step_names:
            prefix = re.sub(r"\d+$", "", want)
            others = [n for n in view.step_names if prefix and prefix != want and n.startswith(prefix)]
            if others:
                misses.append(f"step: expected {want!r}, the page has {others}")
            else:
                unseen.append("step (no control named after the step)")
    if "title" in expect:
        differ("title", expect["title"], view.title)
    if "message_kind" in expect:
        if view.message_kind is not None:
            differ("message_kind", expect["message_kind"], view.message_kind)
        elif not view.message_text:
            differ("message_kind", expect["message_kind"], "")
        else:
            unseen.append("message_kind (the form shows info and error messages alike)")
    if "message" in expect and str(expect["message"]).lower() not in view.message_text.lower():
        misses.append(f"message: expected to contain {expect['message']!r}, got {view.message_text!r}")
    for name, value in (expect.get("fields") or {}).items():
        differ(f"field {name}", value, view.fields.get(name))
    for name, value in (expect.get("labels") or {}).items():
        actual = view.labels.get(name)  # label controls carry their text in the label (S12 reason, S3 question)
        if actual is None or str(value).lower() not in actual.lower():
            misses.append(f"label {name}: expected to contain {value!r}, got {actual!r}")
    return misses, unseen


def exit_button(buttons: list[tuple[str, str]]) -> str | None:
    """The button that leads out, by name or label: Log off, else Cancel (also process-guide
    CancelExitProcess / CancelResetProcess, labelled Cancel), else a "Leave ..." button (Verified
    Inbound's Leave document, the only way out of its scan pages, and the confirming button of its
    "Leave anyway?" page), else Done (a custom flow's S11 after a receipt: the receipt is already
    registered, Done only returns to S1), else Back. Leave before Back: on that confirmation page Back returns to the
    document (logoff went round in circles there, 2026-10-23)."""
    plain = [(name, name.strip().lower(), label.strip().lower()) for name, label in buttons]
    for wanted in ("log off", "cancel"):
        for name, low, label in plain:
            if wanted in (low, label):
                return name
    for name, low, label in plain:
        if low.startswith("cancel") or label.startswith(("log off", "cancel")):
            return name
    for name, _, label in plain:
        if label.startswith("leave"):
            return name
    for name, low, label in plain:  # a custom flow's last page offers only Done (2026-10-27)
        if label == "done" or low.endswith("done"):
            return name
    return next((name for name, low, label in plain if "back" in (low, label)), None)
