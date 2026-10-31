"""The in-form WHS app simulator (`action:WHSWorkExecute`), read into a page d365whsapp's form mode uses.

D38: d365whsapp owns the warehouse app tools; in form mode (never the default) it drives this form
through d365web's WebClient, the owner of the browser. The form runs the warehouse app engine inside
the signed-in web client session, so it needs no Entra app; some things (context classes) behave
slightly differently from the real app (Wald). What the form does not show is reported as not checked
(whs_steps), never as passed: the protocol's page pattern of work pages, the Process Guide step id
(found only when the page has a control named after the step, as a custom flow's labels are), and
whether a message is an error or an info (the form paints both red).
"""

from __future__ import annotations

from typing import Any

from .d365whsapp_steplog import Entry, describe, describe_values as _describe_values, masked
from .whs_steps import View

BUTTON_ROLES = {"CommandButton", "Button", "MenuItemButton"}


def page_from_form(raw: dict[str, Any]) -> dict[str, Any]:
    controls = raw.get("controls") or []
    caption = (raw.get("caption") or "").strip()
    names = {c["name"] for c in controls}
    inputs = {c["name"]: c.get("value", "") for c in controls if "value" in c}
    if {"UserId", "Password"} <= set(inputs):
        pattern = "Login"
    elif caption == "Menu":
        pattern = "Menu"
    else:
        pattern = ""  # a work page: the form does not show the protocol's pattern
    labels = {c["name"]: c.get("text", "") for c in controls if c.get("role") == "Label" and c["name"] != "error"}
    for c in controls:
        if "value" in c and c.get("label"):
            labels.setdefault(c["name"], c["label"])
    page: dict[str, Any] = {"page": pattern or "work", "caption": caption}
    if pattern == "Menu":
        page["title"] = next((t for t in labels.values() if t), "")
    elif pattern != "Login":
        page["title"] = caption
    message = next((c for c in controls if c["name"] == "error" and c.get("text", "").strip()), None)
    if message:
        # no kind: the form paints info and error messages alike (rgb(255, 60, 60), 2026-10-23)
        page["message"] = {"text": message["text"].strip()}
    page["buttons"] = [c["name"] for c in controls if c.get("role") in BUTTON_ROLES]
    page["fields"] = inputs
    page["readonly"] = sorted(c["name"] for c in controls if c.get("readonly"))
    page["labels"] = labels
    page["_names"] = sorted(names)
    page["_button_text"] = {c["name"]: c.get("text", "") for c in controls if c.get("role") in BUTTON_ROLES}
    return page


def public_applied(applied: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Setter diagnostics exposed by form calls must not echo credentials."""
    return [{k: masked(str(item.get("field", "")), v) if k in ("value", "now") else v
             for k, v in item.items()} for item in applied]


def public(page: dict[str, Any]) -> dict[str, Any]:
    result = {k: v for k, v in page.items() if not k.startswith("_") and v not in ({}, [], "")}
    if "fields" in result:
        result["fields"] = {k: masked(k, v) for k, v in result["fields"].items()}
    return result


def view_of(page: dict[str, Any]) -> View:
    message = page.get("message") or {}
    labels = page.get("labels") or {}
    texts = page.get("_button_text") or {}
    return View(
        page=page["page"] if page["page"] in ("Login", "Menu") else None,
        step=None,
        title=page.get("title", ""),
        message_text=message.get("text", ""),
        message_kind=None if message else "",
        fields={**labels, **(page.get("fields") or {})},  # a label control's text counts as its value
        labels=labels,
        buttons=[(name, texts.get(name, "")) for name in page.get("buttons") or []],
        step_names=list(page.get("_names") or []),
    )


def describe_values(values: dict[str, Any] | None, page: dict[str, Any] | None) -> str:
    """{label [name]: value} for the step log, against a form page."""
    return _describe_values(values, (page or {}).get("labels") or {})


def describe_page(page: dict[str, Any]) -> list[str]:
    """The step log's lines for a form page."""
    head = page["page"] + (f' "{page["title"]}"' if page.get("title") else "") + " (form)"
    labels = page.get("labels") or {}
    readonly = set(page.get("readonly") or [])
    fields = page.get("fields") or {}
    entries = [Entry(name, str(labels.get(name, "")), value, readonly=name in readonly)
               for name, value in fields.items()]
    if page["page"] != "Menu":
        entries += [Entry(name, value=label) for name, label in labels.items() if name not in fields and label]
    message = f"message: {page['message'].get('text', '')}" if page.get("message") else ""
    return describe(head, entries, list(page.get("buttons") or []), message)
