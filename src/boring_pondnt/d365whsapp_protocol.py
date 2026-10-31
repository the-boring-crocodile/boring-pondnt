"""The Warehouse Management mobile app protocol, without I/O.

The server (WHSMobileAppServiceFacade.getNextFormHandHeld) answers with one XML
page. The client sends that same page back: every control, in order, with
only the data of enabled input controls changed and the pressed button's data
set to "1". The server stores a check of what it sent and rejects anything
else as REQUEST_XML_TAMPERING.

Trap: the server writes raw TAB characters inside attribute values. An XML
parser normalizes them to spaces, which changes read-only labels. So TAB, LF
and CR inside attribute values are escaped before parsing; ElementTree writes
them back as character references.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

BUTTON_CLICKED = "1"
BUTTON_TYPES = ("button", "detourbutton")
INPUT_TYPES = ("text", "password", "combobox", "checkbox", "date", "listbox")
MESSAGE_CONTROL = "error"
MESSAGE_STATUS_INFO = "3"
XML_DECLARATION = '<?xml version="1.0" encoding="utf-8"?>'

_ATTRIBUTE_VALUE = re.compile(r'="[^"]*"')
_ERROR_CODE = re.compile(r"\b([A-Z][A-Z0-9_]{4,})\s*$")
_ERROR_PREFIX = "An exception occured when invoking the operation - "


class ProtocolError(ValueError):
    """A request that would break the echo rule; raised before anything is sent."""


def escape_attribute_whitespace(xml: str) -> str:
    """Escape TAB, LF and CR inside attribute values so a parser keeps them."""
    def escape(match: re.Match[str]) -> str:
        return match.group(0).replace("\t", "&#9;").replace("\n", "&#10;").replace("\r", "&#13;")
    return _ATTRIBUTE_VALUE.sub(escape, xml)


@dataclass
class Control:
    type: str
    name: str
    label: str
    data: str
    enabled: bool
    error: bool
    selected: str
    attached_to: str
    length: int

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type, "name": self.name}
        if self.label and self.label.strip() != self.name:
            out["label"] = self.label.strip()
        if self.data and self.type != "password":
            out["data"] = self.data
        if self.selected:
            out["selected"] = self.selected
        if not self.enabled:
            out["readonly"] = True
        if self.error:
            out["error"] = True
        if self.attached_to:
            out["attached_to"] = self.attached_to
        return out


@dataclass
class Page:
    """One server page. `root` is the parsed XML that the next request echoes."""
    root: ET.Element
    pattern: str
    title: str
    menu_item: str
    detour: bool
    controls: list[Control]
    step: dict[str, str] = field(default_factory=dict)
    message: dict[str, str] | None = None
    user: str = ""
    session: bool = False
    warehouse: dict[str, str] = field(default_factory=dict)
    device_id: str = ""

    def control(self, name: str) -> Control | None:
        return next((c for c in self.controls if c.name == name), None)

    @property
    def is_login(self) -> bool:
        return self.pattern == "Login"

    def inputs(self) -> list[str]:
        return [c.name for c in self.controls if c.enabled and c.type in INPUT_TYPES]

    def buttons(self) -> list[str]:
        return [c.name for c in self.controls if c.enabled and c.type in BUTTON_TYPES]

    def summary(self) -> dict[str, Any]:
        """Compact view for tools. The header title only on work pages: it is
        sticky on menus and the login page (P0); a menu's title is its label."""
        out: dict[str, Any] = {"page": self.pattern}
        if self.pattern == "Menu":
            title = next((c.label for c in self.controls if c.type == "label" and c.name != MESSAGE_CONTROL), "")
            out["title"] = title.strip()
        elif self.pattern != "Login":
            out["title"] = self.title
        if self.step:
            out["step"] = self.step
        if self.message:
            out["message"] = self.message
        if self.detour:
            out["detour"] = True
        if self.pattern == "Menu":
            out["buttons"] = self.buttons()
        else:
            out["controls"] = [c.summary() for c in self.controls if c.name != MESSAGE_CONTROL]
        if self.warehouse.get("Id"):
            out["warehouse"] = self.warehouse["Id"]
        return out


def _flag(value: str | None) -> bool:
    return value == "1"


def _int(value: str | None) -> int:
    try:
        return int(value or "")
    except ValueError:
        return -1


def parse_page(xml: str) -> Page:
    """Parse a server page. Raises ProtocolError for anything that is not one."""
    text = xml.lstrip("﻿").strip()
    if not text:
        raise ProtocolError("empty page")
    try:
        root = ET.fromstring(escape_attribute_whitespace(text))
    except ET.ParseError as exc:
        raise ProtocolError(f"page is not XML: {exc}") from None
    if root.tag != "ParentNode":
        raise ProtocolError(f"page root is {root.tag}, expected ParentNode")
    controls_node = root.find("Controls")
    if controls_node is None:
        raise ProtocolError("page has no Controls")
    controls: list[Control] = []
    message = None
    for node in controls_node.findall("Control"):
        a = node.attrib
        control = Control(type=a.get("controlType", ""), name=a.get("name", ""), label=a.get("label", ""),
                          data=a.get("data", ""), enabled=_flag(a.get("enabled")), error=_flag(a.get("error")),
                          selected=a.get("selected", ""), attached_to=a.get("AttachedTo", ""),
                          length=_int(a.get("length")))
        controls.append(control)
        if control.name == MESSAGE_CONTROL and control.type == "label":
            kind = "info" if a.get("Status") == MESSAGE_STATUS_INFO else "error"
            message = {"kind": kind, "text": control.label.strip()}
    step_node = root.find("Step")
    step = {}
    if step_node is not None:
        step = {k: v for k, v in (("id", step_node.get("Id", "")), ("title", step_node.get("Title", "")),
                                  ("instruction", step_node.get("Instruction", ""))) if v}
    auth = root.find("Auth")
    warehouse = root.find("Warehouse")
    device = root.find("Device")
    c = controls_node.attrib
    return Page(root=root, pattern=c.get("PagePattern", ""), title=c.get("PageTitle", ""),
                menu_item=c.get("MenuItemName", ""), detour=_flag(c.get("IsInDetourSession")),
                controls=controls, step=step, message=message,
                user=auth.get("userId", "") if auth is not None else "",
                session=bool(auth is not None and auth.get("sessionId")),
                warehouse=dict(warehouse.attrib) if warehouse is not None else {},
                device_id=device.get("DeviceId", "") if device is not None else "")


def build_request(page: Page, values: dict[str, str] | None = None, press: str | None = None,
                  device_id: str | None = None) -> str:
    """The page echoed back with `values` filled in and `press` clicked.

    Refuses locally what the server would reject or misread: unknown, read-only
    or non-input fields, values longer than the field, a button that is not an
    enabled button. The page itself is not changed.
    """
    values = dict(values or {})
    by_name = {c.name: c for c in page.controls}
    for name, value in values.items():
        control = by_name.get(name)
        if control is None:
            raise ProtocolError(f"no field {name!r} on this page; fields: {', '.join(page.inputs()) or 'none'}")
        if control.type not in INPUT_TYPES:
            raise ProtocolError(f"{name!r} is a {control.type}, not an input field")
        if not control.enabled:
            raise ProtocolError(f"{name!r} is read-only on this page")
        if not isinstance(value, str):
            raise ProtocolError(f"value for {name!r} must be a string")
        if control.length > 0 and len(value) > control.length:
            raise ProtocolError(f"{name!r} takes at most {control.length} characters")
    if press is not None:
        control = by_name.get(press)
        if control is None or control.type not in BUTTON_TYPES:
            raise ProtocolError(f"no button {press!r} on this page; buttons: {', '.join(page.buttons()) or 'none'}")
        if not control.enabled:
            raise ProtocolError(f"button {press!r} is disabled")
    root = _copy(page.root)
    for node in root.find("Controls").findall("Control"):  # type: ignore[union-attr]
        name = node.get("name", "")
        if name in values:
            node.set("data", values[name])
        if press is not None and name == press:
            node.set("data", BUTTON_CLICKED)
    if device_id:
        device = root.find("Device")
        if device is None:
            device = ET.SubElement(root, "Device")
        device.set("DeviceId", device_id)
    return XML_DECLARATION + ET.tostring(root, encoding="unicode")


def _copy(element: ET.Element) -> ET.Element:
    return ET.fromstring(ET.tostring(element, encoding="unicode"))


@dataclass
class ServiceError:
    """An HTTP failure of the service. `code` is the protocol code at the end of
    the message (REQUEST_XML_TAMPERING, REQUEST_SESSION_TOKEN_ERROR,
    MULTI_DEVICE_USER_LOGIN, ...) or "" when there is none."""
    status: int
    code: str
    message: str

    @property
    def needs_login(self) -> bool:
        return self.code in ("REQUEST_SESSION_TOKEN_ERROR", "MULTI_DEVICE_USER_LOGIN")


def parse_service_error(status: int, body: str) -> ServiceError:
    message = body.strip()
    try:
        data = json.loads(message)
        if isinstance(data, dict):
            message = str(data.get("Message") or (data.get("error") or {}).get("message") or message)
    except ValueError:
        pass
    if message.startswith(_ERROR_PREFIX):
        message = message[len(_ERROR_PREFIX):]
    match = _ERROR_CODE.search(message)
    return ServiceError(status=status, code=match.group(1) if match else "", message=message[:1000])
