"""d365web's D365 layer: drive the Finance and Operations web client like a person, through d365web_browser.

Everything is found through the client's own markup: every control carries data-dyn-controlname and
data-dyn-role, forms carry data-dyn-form-name, lookups are [data-dyn-role=Popup].lookup-popup, dialogs and
message boxes are [role=dialog], and messages are [data-dyn-role=MessageBarSection] (severity in the class).
"Idle" is $dyn.clientBusy() false for a moment (the client keeps a long-poll open, so network idle is useless).

Scope rule: hidden forms stay in the DOM, so every lookup by name happens in the top-most visible container:
lookup/popup, else the last visible dialog, else the largest visible form, including the detached
action flyout of an expanded tab in that container. `nth` counts matches in visual
order (top to bottom, then left to right); grid cells count only rows inside the grid's viewport.

All browser work runs on one worker thread. A call waits up to `budget` seconds; a step that takes longer
keeps running and the call answers {"status": "running"}; web_wait collects the result.
"""

from __future__ import annotations

import json
from importlib import resources
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, fields
from functools import partial
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlsplit

from . import scenario_values
from .d365whsapp_steplog import SECRET_NAME_PATTERN, MASK, secret
from .collect import Run, Runs, StillRunning  # noqa: F401 -- StillRunning: d365web's callers catch it here
from . import d365web_model
from .d365web_browser import Browser, BrowserError, Page

PROFILE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
MENU_KINDS = ("display", "action", "output")
MENU_ITEM = re.compile(r"^(?:(?:display|action|output):)?[A-Za-z0-9_]{1,81}$")
FIELD_KEY = re.compile(r"^(?P<name>.+?)(?:\[(?P<nth>\d{1,3})\])?$")
SETTLE_SECONDS = 0.4
GRID_SCAN_CAP = 1000  # rows a grid scan collects at most (web_grid all_rows, select_row match, expect grid)
READONLY_GRACE = 3.0  # D365 enables dependent fields (warehouse after site) a moment later
TRUE_WORDS = {"yes", "true", "1", "on", "ja"}
FALSE_WORDS = {"no", "false", "0", "off", "nein", ""}


class D365WebError(Exception):
    """`code`: signin_required, not_found, refused, busy, no_lookup, browser, config."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.details = code, message, details


def default_profiles_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "boring-pondnt" / "d365web" / "profiles"


@dataclass
class Config:
    url: str
    company: str = ""  # empty: D365 opens the user's default company
    profile: str = "default"
    profiles_dir: str | None = None
    headless: bool = True
    edge: str | None = None
    idle_minutes: float = 30.0
    step_timeout: float = 600.0  # D365 forms and postings can run for minutes; web_wait collects
    debug_port: int = 0  # fixed loopback port: an Edge that outlives the server is found again
    backend: str = "model"  # model: D365's control viewmodels; input: synthetic clicks and keys (d365web_model)

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"{path}: unknown keys {unknown}")
        config = cls(**data)
        config.validate()
        return config

    def validate(self) -> None:
        parts = urlsplit(self.url)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError(f"url must be https://<host>: {self.url!r}")
        if self.debug_port and not 1024 <= int(self.debug_port) <= 65535:
            raise ValueError(f"debug_port must be 1024..65535: {self.debug_port!r}")
        if self.company and not re.match(r"^[A-Za-z0-9]{1,8}$", self.company):
            raise ValueError(f"company must be a D365 company id: {self.company!r}")
        if self.backend not in d365web_model.BACKENDS:
            raise ValueError(f"backend must be one of {list(d365web_model.BACKENDS)}: {self.backend!r}")
        if not PROFILE_NAME.match(self.profile):
            raise ValueError(f"profile must match {PROFILE_NAME.pattern}: {self.profile!r}")
        self.url = f"https://{parts.netloc}"

    def profile_dir(self) -> Path:
        return Path(self.profiles_dir) if self.profiles_dir else default_profiles_dir() / self.profile


# --- the page-side library --------------------------------------------------------------------------
# Called as (LIB)(op, args). Returns plain JSON. Kept free of D365 globals except $dyn.clientBusy.

# The page-side library: one expression, called as (LIB)(op, args, secretNamePattern); returns plain JSON.
# Kept in its own file (d365web_lib.js) so it reads, diffs and lints as JavaScript.
LIB = (resources.files(__package__) / "d365web_lib.js").read_text(encoding="utf-8").strip()


def iso_matches(want: str, raw: str) -> bool:
    """D365's raw value of a date (2026-11-20, 2026-11-20T00:00:00) or date-time holds the ISO value set."""
    raw = raw.strip()
    if raw == want:
        return True
    if not raw.startswith(want):
        return False
    rest = raw[len(want):]
    # a date may carry a midnight time; a date-time given to the minute may carry zero seconds
    return rest[:1] in (("T",) if "T" not in want else (":", ".", "Z")) and not rest[1:].strip("0:.Z")


def field_key_explicit(key: str) -> bool:
    """Whether a field key names its occurrence ('Name[0]'): then no active-row choice is made."""
    match = FIELD_KEY.match(key.strip())
    return bool(match and match.group("nth") is not None)


def parse_field_key(key: str) -> tuple[str, int]:
    match = FIELD_KEY.match(key.strip())
    if not match or not match.group("name"):
        raise D365WebError("refused", f"bad field name {key!r}")
    return match.group("name"), int(match.group("nth") or 0)


def normalize_values(values: Any) -> list[tuple[str, Any]]:
    if isinstance(values, dict):
        pairs = list(values.items())
    elif isinstance(values, list):
        pairs = []
        for item in values:
            if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], str):
                pairs.append((item[0], item[1]))
            elif isinstance(item, dict) and set(item) == {"field", "value"} and isinstance(item["field"], str):
                pairs.append((item["field"], item["value"]))
            else:
                raise D365WebError("refused", f"values: expected [name, value] pairs, got {item!r}")
    else:
        raise D365WebError("refused", "values must be an object or a list of [name, value] pairs")
    if not pairs:
        raise D365WebError("refused", "values is empty")
    return pairs


_DATE_OR_TIME = re.compile(r"^\d{1,4}[/.:]\d")
_THOUSANDS = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$")


def _number(text: str) -> float | None:
    """A number as the en-US client shows it; '1,5' is not 15 (a decimal comma is compared as text)."""
    text = text.strip()
    if "," in text:
        if not _THOUSANDS.match(text):
            return None
        text = text.replace(",", "")
    try:
        return float(text)
    except ValueError:
        return None


def value_differs(typed: str, now: str) -> bool:
    """True when a field does not hold what was typed, allowing D365's own formatting:
    125 -> 125.00, 618160 -> 618160-001--- (segments), case and spacing."""
    a, b = typed.strip(), now.strip()
    if a.lower() == b.lower():
        return False
    if not a:
        return bool(b)
    if not b:
        return True
    x, y = _number(a), _number(b)
    if x is not None and y is not None:
        return x != y
    # a segmented entry fills its remaining segments (618160 -> 618160-001---); any other longer value is
    # a different value (D365 completed ABC to ABC1)
    if not b.lower().startswith(a.lower()):
        return True
    rest = b[len(a):]
    # 11/6/2026 -> "11/6/2026 12:00:00 am", 10:00 -> "10:00 am": D365 completes a date or a time
    return not (rest[:1] == "-" or (rest[:1] == " " and _DATE_OR_TIME.match(a) is not None))


SCENARIO_STEPS_MAX = 80
STEP_ARGS = {
    "open": {"menu_item", "company", "kind"}, "state": {"full", "max_rows"}, "set": {"values"},
    "press": {"name", "nth"}, "lookup": {"field", "pick", "nth"}, "filter": {"value", "column"},
    "select_row": {"grid", "row", "match"}, "key": {"keys"}, "tree": {"tree", "path"},
}
TREE_DEPTH_MAX = 20
STEP_KEYS = {"op", "expect", "save", "note"}
EXPECT_KEYS = {"scope", "form", "caption", "fields", "editable", "message", "no_errors", "enabled", "disabled", "grid"}


def _refused(message: str) -> D365WebError:
    return D365WebError("refused", message)


substitute = partial(scenario_values.substitute, refuse=_refused)


def _step_text(value: Any, name: str, *, empty: bool = False) -> None:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise D365WebError("refused", f"{name}: expected {'text' if empty else 'nonempty text'}")


def _step_int(value: Any, name: str, minimum: int = 0, *, templates: bool = False) -> None:
    if isinstance(value, str):
        if templates and scenario_values.VARIABLE.fullmatch(value):
            return  # the page-dependent saved value is checked again before its step
        if re.fullmatch(r"[+-]?[0-9]+", value):
            value = int(value)
    if type(value) is not int or value < minimum:
        raise D365WebError("refused", f"{name}: expected an integer >= {minimum}")


def _step_map(value: Any, name: str) -> None:
    scenario_values.check_scalar_map(value, name, _refused)


def check_operation(op: str, args: dict[str, Any], *, templates: bool = False) -> None:
    """Static operation arguments, shared by scenario admission and direct entry points.
    Browser-dependent names/availability remain runtime checks."""
    required = {"open": "menu_item", "set": "values", "press": "name", "lookup": "field",
                "key": "keys", "tree": "path"}
    if op in required and required[op] not in args:
        raise D365WebError("refused", f"{op}: missing {required[op]}")
    for name in ("menu_item", "name", "field", "keys"):
        if name in args:
            _step_text(args[name], name)
    if op == "tree":
        path = args["path"]
        if not isinstance(path, list) or not 1 <= len(path) <= TREE_DEPTH_MAX:
            raise D365WebError("refused", f"path: expected a list of 1-{TREE_DEPTH_MAX} node labels, root first")
        for label in path:
            _step_text(label, "path")
    for name in ("company", "kind", "pick", "column", "grid", "tree"):
        if name in args and args[name] is not None:
            _step_text(args[name], name, empty=name != "kind")
    if op == "open":
        menu = args["menu_item"]
        company, kind = args.get("company"), args.get("kind")
        if templates:
            menu = scenario_values.VARIABLE.sub("X", menu)
            company = scenario_values.VARIABLE.sub("X", company) if company else company
            kind = scenario_values.VARIABLE.sub("display", kind) if kind else kind
        menu = f"{kind}:{menu}" if kind and kind != "display" else menu
        if not MENU_ITEM.fullmatch(menu):
            raise D365WebError("refused", "menu_item: invalid menu item")
        if company and not re.fullmatch(r"[a-zA-Z0-9]{1,8}", company):
            raise D365WebError("refused", "company: expected 1-8 letters or digits")
        if kind is not None and kind not in MENU_KINDS:
            raise D365WebError("refused", "kind: expected display, action or output")
    for name in ("nth", "row", "max_rows"):
        if name in args:
            _step_int(args[name], name, 1 if name == "max_rows" else 0, templates=templates)
    if "full" in args and type(args["full"]) is not bool:
        raise D365WebError("refused", "full: expected a boolean")
    if "value" in args:
        _step_text(args["value"], "value", empty=True)
    if op == "set":
        for name, value in normalize_values(args["values"]):
            _step_text(name, "field")
            parse_field_key(name)
            _step_map({name: value}, "values")
    if args.get("match") is not None:
        _step_map(args["match"], "match")


def _check_web_expect(expect: Any, *, templates: bool = False) -> None:
    if not isinstance(expect, dict):
        raise D365WebError("refused", "expect must be an object")
    bad = set(expect) - EXPECT_KEYS
    if bad:
        raise D365WebError("refused", f"unknown expect keys {sorted(bad)}; use {sorted(EXPECT_KEYS)}")
    for name in ("scope", "form", "caption", "message"):
        if name in expect:
            _step_text(expect[name], name, empty=True)
    if "fields" in expect:
        _step_map(expect["fields"], "expect.fields")
    for name in ("editable", "enabled", "disabled"):
        if name in expect:
            if not isinstance(expect[name], list):
                raise D365WebError("refused", f"expect.{name}: expected a list of names")
            for item in expect[name]:
                _step_text(item, f"expect.{name}")
    if "no_errors" in expect and type(expect["no_errors"]) is not bool:
        raise D365WebError("refused", "expect.no_errors: expected a boolean")
    if "grid" in expect:
        grid = expect["grid"]
        if not isinstance(grid, dict) or set(grid) - {"name", "rows", "contains"}:
            raise D365WebError("refused", "expect.grid: expected an object with name/rows/contains")
        if "name" in grid:
            _step_text(grid["name"], "expect.grid.name", empty=True)
        if "rows" in grid:
            _step_int(grid["rows"], "expect.grid.rows", templates=templates)
        if "contains" in grid:
            _step_map(grid["contains"], "expect.grid.contains")


def _check_web_save(save: Any) -> None:
    if not isinstance(save, dict):
        raise D365WebError("refused", "save must be an object")
    for name, spec in save.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise D365WebError("refused", "save: invalid variable name")
        if isinstance(spec, str):
            spec = {"from": spec}
        if not isinstance(spec, dict) or set(spec) - {"from", "regex", "row", "grid"}:
            raise D365WebError("refused", "save specification: expected from/regex/row/grid")
        source = spec.get("from")
        if not isinstance(source, str) or not (
                source in ("caption", "message") or re.fullmatch(r"(field|grid):.+", source)):
            raise D365WebError("refused", "save.from: expected caption, message, field:NAME or grid:COLUMN")
        if "row" in spec:
            _step_int(spec["row"], "save.row")
        if "grid" in spec:
            _step_text(spec["grid"], "save.grid", empty=True)
        if "regex" in spec:
            _step_text(spec["regex"], "save.regex", empty=True)
            try:
                re.compile(spec["regex"])
            except re.error:
                raise D365WebError("refused", "save.regex: invalid regular expression") from None


def check_steps(steps: Any, variables: dict[str, Any] | None = None) -> None:
    if not isinstance(steps, list) or not steps:
        raise D365WebError("refused", "steps is empty")
    if len(steps) > SCENARIO_STEPS_MAX:
        raise D365WebError("refused", f"at most {SCENARIO_STEPS_MAX} steps per scenario")
    available = {k: str(v) for k, v in (variables or {}).items()}
    for index, step in enumerate(steps, 1):
        try:
            if not isinstance(step, dict) or not isinstance(step.get("op"), str) or step["op"] not in STEP_ARGS:
                raise D365WebError("refused", f"op must be one of {sorted(STEP_ARGS)}")
            op = step["op"]
            unknown = set(step) - STEP_KEYS - STEP_ARGS[op]
            if unknown:
                raise D365WebError("refused", f"unknown keys {sorted(unknown)}")
            args = {k: v for k, v in step.items() if k in STEP_ARGS[op]}
            args, expect = substitute([args, step.get("expect", {})], available)
            check_operation(op, args, templates=True)
            _check_web_expect(expect, templates=True)
            _check_web_save(step.get("save", {}))
            if "note" in step:
                _step_text(step["note"], "note", empty=True)
            # Saved page data is not known yet; preserve its placeholder for shape checks.
            available.update({name: "${" + name + "}" for name in step.get("save", {})})
        except D365WebError as error:
            raise D365WebError(error.code, f"step {index}: {error.message}") from None


def same_value(expected: Any, actual: str) -> bool:
    a, b = str(expected).strip(), str(actual).strip()
    if a.lower() == b.lower():
        return True
    x, y = _number(a), _number(b)
    return x is not None and y is not None and x == y


def _field_value(state: dict[str, Any], name: str) -> str | None:
    """A field by control name or label, editable or read-only."""
    for group in ("fields", "readonly"):
        if name in state.get(group, {}):
            return state[group][name]
    for key, label in (state.get("labels") or {}).items():
        if label.strip().lower() == name.strip().lower():
            for group in ("fields", "readonly"):
                if key in state.get(group, {}):
                    return state[group][key]
    return None


def _grid_rows(state: dict[str, Any], grid_name: str | None) -> tuple[list[dict], list[dict]] | None:
    grids = state.get("grids") or []
    grid = next((g for g in grids if g.get("name") == grid_name), None) if grid_name else (grids[0] if grids else None)
    if grid is None:
        return None
    return grid.get("columns", []), grid.get("rows", [])


def _row_matches(columns: list[dict], row: dict, match: dict[str, Any]) -> bool:
    for column, wanted in match.items():
        index = next((i for i, c in enumerate(columns)
                      if c.get("name") == column or (c.get("label") or "").lower() == column.lower()), None)
        if index is None or index >= len(row["values"]) or not same_value(wanted, row["values"][index]):
            return False
    return True


def check_expectation(state: dict[str, Any], expect: dict[str, Any]) -> list[str]:
    misses: list[str] = []
    for key in ("scope", "form"):
        if key in expect and str(state.get(key, "")) != str(expect[key]):
            misses.append(f"{key}: expected {expect[key]!r}, got {state.get(key, '')!r}")
    if "caption" in expect and str(expect["caption"]).lower() not in str(state.get("caption", "")).lower():
        misses.append(f"caption: expected to contain {expect['caption']!r}, got {state.get('caption', '')!r}")
    for name, wanted in (expect.get("fields") or {}).items():
        actual = _field_value(state, name)
        if actual is None:
            misses.append(f"field {name}: not on the page")
        elif not same_value(wanted, actual):
            misses.append(f"field {name}: expected {wanted!r}, got {actual!r}")
    for name in expect.get("editable") or []:
        if name not in (state.get("fields") or {}):
            where = "read-only" if name in (state.get("readonly") or {}) else "not on the page"
            misses.append(f"editable: {name} is {where}")
    new = state.get("new_messages") or []
    if "message" in expect:
        texts = [m["text"] for m in new] or [m["text"] for m in state.get("messages") or []]
        if not any(str(expect["message"]).lower() in t.lower() for t in texts):
            misses.append(f"message: none contains {expect['message']!r} (got {texts[:3]})")
    if expect.get("no_errors"):
        bad = [m["text"] for m in new if m["severity"] in ("error", "warning")]
        if bad:
            misses.append(f"no_errors: {bad[:3]}")
    for key, group in (("enabled", "buttons"), ("disabled", "disabled")):
        names = set(state.get(group) or []) | set(state.get("menu") or [])
        for name in expect.get(key) or []:
            labels = {v.lower(): k for k, v in (state.get("labels") or {}).items()}
            if name not in names and labels.get(name.lower()) not in names:
                misses.append(f"{key}: {name!r} is not {key}")
    if "grid" in expect:
        spec = expect["grid"] or {}
        found = _grid_rows(state, spec.get("name"))
        if found is None:
            misses.append(f"grid {spec.get('name') or '(first)'}: not on the page")
        else:
            columns, rows = found
            if "rows" in spec and len(rows) != int(spec["rows"]):
                misses.append(f"grid rows: expected {spec['rows']}, got {len(rows)}")
            if spec.get("contains") and not any(_row_matches(columns, r, spec["contains"]) for r in rows):
                misses.append(f"grid: no row with {spec['contains']}")
    return misses


def extract_variable(state: dict[str, Any], spec: Any) -> str | None:
    """save spec: "caption" | "field:NAME" | "message" | "grid:COLUMN" or {"from": ..., "regex": ..., "row": n}."""
    if isinstance(spec, str):
        spec = {"from": spec}
    source, pattern = str(spec.get("from", "")), spec.get("regex")
    text: str | None = None
    if source == "caption":
        text = state.get("caption")
    elif source.startswith("field:"):
        text = _field_value(state, source[6:])
    elif source == "message":
        text = "\n".join(m["text"] for m in (state.get("new_messages") or state.get("messages") or []))
    elif source.startswith("grid:"):
        for grid in state.get("grids") or []:
            if spec.get("grid") and grid.get("name") != spec["grid"]:
                continue
            columns, rows = grid.get("columns", []), grid.get("rows", [])
            index = next((i for i, c in enumerate(columns) if c.get("name") == source[5:]
                          or (c.get("label") or "").lower() == source[5:].lower()), None)
            if index is None:
                continue
            row = int(spec.get("row", 0))
            if 0 <= row < len(rows) and index < len(rows[row]["values"]):
                text = rows[row]["values"][index]
            break
    if text is None:
        return None
    if pattern:
        match = re.search(pattern, text)
        if not match:
            return None
        return match.group(1) if match.groups() else match.group(0)
    return text


def as_bool(value: str) -> bool:
    word = str(value).strip().lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise D365WebError("refused", f"checkbox value must be yes/no, got {value!r}")


def context_moves(before: Any, after: Any) -> list[str] | None:
    """What moved between two page contexts (the page library's `context`): form (the visible forms),
    dialog (the one in front), record (caption or a grid's active row), pane (action tab, menu, FastTabs,
    selected tabs), fields (values or read-only state in the scope). None: a context was unreadable,
    so nothing can be said either way."""
    if not isinstance(before, dict) or not isinstance(after, dict) or "error" in before or "error" in after:
        return None
    moved = ["form"] if before.get("forms") != after.get("forms") else []
    return moved + [key for key in ("dialog", "record", "pane", "fields") if before.get(key) != after.get(key)]


UNCHANGED_NOTE = ("nothing visible moved (form, dialog, active record, pane, fields, messages, address): D365 "
                  "may have done nothing, or work that shows nowhere here; inspect (web_state) before "
                  "pressing again: a business action is never retried for you")


BROWSER = "browser"  # WebClient's one run key: one browser, one step at a time


def _busy(run: Run) -> D365WebError:
    if not run.future.done():
        return D365WebError("busy", f"{run.label} is still running; call web_wait first")
    return D365WebError("busy", f"{run.label} has finished and its result has not been collected; "
                                "call web_wait for it first")


@dataclass
class WebClient:
    config: Config
    browser_factory: Callable[[Config], Browser] | None = None
    budget: float = 25.0
    state_dir: Path | None = None
    browser: Browser | None = field(default=None, init=False)
    page: Page | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="d365web")
        self.runs = Runs(self.executor)  # one key: the browser (collect: answered once, by its collector)
        self.last_used = time.monotonic()
        self.js_dialogs: list[str] = []
        self.js_dialogs_dismissed: list[str] = []  # confirm/prompt dialogs cancelled during the step
        self.dismissed: list[str] = []  # teaching tips acknowledged during the current step (see _uncover)
        self.keep_open: Callable[[], bool] | None = None  # true: the idle close waits (another owner's tabs)
        self._idle_unknown = 0  # idle closes put off because the page did not say whether it holds unsaved changes
        self.stop = threading.Event()
        self.reaper = threading.Thread(target=self._reap_idle, name="d365web-idle", daemon=True)
        self.reaper.start()

    # --- the worker -------------------------------------------------------------------------------
    def call(self, label: str, fn: Callable[[], Any], budget: float | None = None, owner: Any = None) -> Any:
        """Run fn on the worker; wait up to budget, else StillRunning. Refused (busy) while an earlier
        step is open: still running, or finished and not collected by web_wait (collect.Runs).
        `owner` stays with the run (form mode: the worker whose step it is)."""
        run = self.runs.start(BROWSER, label, self._guarded, fn, refuse=_busy, owner=owner)
        return self._collect(run, budget if budget is not None else self.budget)

    def wait(self, seconds: float | None = None) -> Any:
        """The open step's result (waits up to seconds), or the current state when nothing is open."""
        run = self.runs.get(BROWSER)
        if run is None:
            return self.call("web_wait", lambda: self._settle_and_state(), seconds)
        return self._collect(run, seconds if seconds is not None else self.budget)

    def _collect(self, run: Run, budget: float) -> Any:
        try:
            return self.runs.collect(BROWSER, run, budget)
        finally:
            self.last_used = time.monotonic()

    def _guarded(self, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except BrowserError as error:
            if self.browser is not None and self.browser.conn.closed:
                self.browser, self.page = None, None
            raise D365WebError("browser", error.message, browser_code=error.code) from None
        finally:
            self.last_used = time.monotonic()

    def _reap_idle(self) -> None:
        while not self.stop.wait(30.0):
            idle = time.monotonic() - self.last_used
            if self.browser is not None and idle > self.config.idle_minutes * 60:
                run = self.runs.get(BROWSER)
                if run is None or run.future.done():
                    self.executor.submit(self._close_idle)

    def shutdown(self) -> None:
        self.stop.set()
        self.executor.submit(self._close_browser).result(timeout=30)
        self.executor.shutdown(wait=False)

    # --- browser lifetime (worker thread only) ----------------------------------------------------
    def _connect(self) -> Browser:
        if self.browser_factory is not None:
            return self.browser_factory(self.config)
        profile = self.config.profile_dir()
        ports = [self.config.debug_port] if self.config.debug_port else []
        try:
            ports.append(int((profile / "DevToolsActivePort").read_text(encoding="utf-8").split()[0]))
        except (OSError, ValueError, IndexError):
            pass
        for port in ports:
            try:
                return Browser.attach(port, profile)
            except BrowserError as error:
                if error.code == "foreign" and port == self.config.debug_port:
                    # launching on that port would end up talking to the same foreign Edge
                    raise D365WebError("browser", f"another Edge (another profile) uses debugging port {port}; "
                                       "close it or configure another debug_port") from None
                continue
            except (OSError, ValueError, KeyError):
                continue
        return Browser.launch(profile, headless=self.config.headless, edge=self.config.edge,
                              port=self.config.debug_port)

    def _ensure(self) -> Page:
        if self.browser is None or self.browser.conn.closed:
            self.browser, self.page = self._connect(), None
        if self.page is None:
            targets = self.browser.conn.send("Target.getTargets")["targetInfos"]
            pages = [t for t in targets if t["type"] == "page" and not t.get("url", "").startswith(("edge://", "devtools://", "chrome://"))
                     and "WHSWorkExecute" not in t.get("url", "")]  # the form tabs belong to d365whsapp (D38)
            # an attached headed Edge may hold other tabs: prefer the one on this D365
            ours = [t for t in pages if t.get("url", "").startswith(self.config.url)]
            chosen = (ours or pages)[:1]
            self.page = Page(self.browser.conn, chosen[0]["targetId"]) if chosen else self.browser.new_page()
        return self.page

    def _close_idle(self) -> None:
        """The idle close: never while a form holds unsaved changes (they would be lost without a word).
        A browser that is gone is closed; a page that does not answer is waited for three times."""
        if self.browser is None or time.monotonic() - self.last_used <= self.config.idle_minutes * 60:
            return
        if self.keep_open is not None and not self.browser.conn.closed and self.keep_open():
            self.last_used = time.monotonic()  # a tab holds a logged-in session (d365whsapp form mode)
            return
        if self.page is not None and not self.browser.conn.closed:
            try:
                unsaved = d365web_model.unsaved_forms(lambda op, *a: self._evaluate_lib(op, *a), strict=True)
            except Exception:  # noqa: BLE001 -- unreadable: as below
                unsaved = None
            if unsaved or (unsaved is None and self._idle_unknown < 3):
                self._idle_unknown = self._idle_unknown + 1 if unsaved is None else 0
                self.last_used = time.monotonic()  # look again in idle_minutes
                return
        self._idle_unknown = 0
        self._close_browser()

    def _close_browser(self) -> dict[str, Any]:
        browser, self.browser, self.page = self.browser, None, None
        if browser is None:
            return {"closed": False, "reason": "no browser"}
        # the profile is d365web's own: an Edge attached on it (left from an earlier start) is closed too
        browser.close()
        return {"closed": True}

    # --- page helpers (worker thread only) --------------------------------------------------------
    def _js(self, op: str, *args: Any) -> Any:
        page = self._ensure()
        if op.startswith("model:"):  # the model backend's library (d365web_model); same route to the page
            return page.evaluate(f"({d365web_model.MODEL_LIB})({json.dumps(op[6:])}, {json.dumps(list(args))})")
        return page.evaluate(f"({LIB})({json.dumps(op)}, {json.dumps(list(args))}, {json.dumps(SECRET_NAME_PATTERN)})")

    def _evaluate_lib(self, op: str, *args: Any) -> Any:
        """_js on the current page only: never launches or attaches a browser (the idle close asks)."""
        if self.page is None:
            raise BrowserError("no_page", "no page")
        library = d365web_model.MODEL_LIB if op.startswith("model:") else LIB
        name = op[6:] if op.startswith("model:") else op
        extra = "" if op.startswith("model:") else f", {json.dumps(SECRET_NAME_PATTERN)}"
        return self.page.evaluate(f"({library})({json.dumps(name)}, {json.dumps(list(args))}{extra})")

    def _accept_js_dialogs(self) -> None:
        page = self.page
        if page is None:
            return
        with page.events_lock:
            dialogs, page.dialogs = page.dialogs, []
        for dialog in dialogs:
            kind = dialog.get("type", "")
            # A confirm (or prompt) may approve something nobody asked for: cancel it (Wald, 2026-11-07).
            # An alert only informs; a leave-page question is answered yes, as web_open already refuses
            # while a form holds unsaved changes.
            accept = kind not in ("confirm", "prompt")
            (self.js_dialogs if accept else self.js_dialogs_dismissed).append(
                f"{kind}: {dialog.get('message', '')}"[:300])
            try:
                page.send("Page.handleJavaScriptDialog", {"accept": accept})
            except BrowserError:
                pass

    def _wait_idle(self, timeout: float | None = None) -> bool:
        deadline = time.monotonic() + (timeout if timeout is not None else self.config.step_timeout)
        calm: float | None = None
        while time.monotonic() < deadline:
            self._accept_js_dialogs()
            status = self._js("status")
            if status == "signin":
                raise D365WebError("signin_required",
                                   f"profile {self.config.profile!r} is not signed in: run 'd365web sign-in' on the VM desktop")
            if status == "idle":
                calm = calm or time.monotonic()
                if time.monotonic() - calm >= SETTLE_SECONDS:
                    return True
            else:
                calm = None
            time.sleep(0.1)
        return False

    def browser_mode(self) -> dict[str, Any]:
        actual = self.browser.headless if self.browser is not None else None
        if actual is None:
            return {"browser_mode_unknown": True}
        if actual != self.config.headless:
            return {"browser_mode_mismatch": {
                "configured_headless": self.config.headless, "running_headless": actual,
                "hint": "Finish and collect the current operation, close the service browser, then relaunch.",
            }}
        return {}

    def _state(self, full: bool = False, max_rows: int = 20, idle: bool = True) -> dict[str, Any]:
        page = self._ensure()
        result: dict[str, Any] = {}
        result.update(self._js("state", full, max_rows))
        unsaved = d365web_model.unsaved_forms(self._js)  # a read: reported by both backends
        if unsaved:
            result["unsaved"] = unsaved
        result.update(self.browser_mode())
        href = page.url()
        parts = urlsplit(href)
        query = parse_qs(parts.query)
        result["url"] = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        if query.get("cmp"):
            result["company"] = query["cmp"][0]
        if not idle:
            result["idle"] = False
            result["note"] = f"the client was still busy after {self.config.step_timeout:.0f} s; call web_wait"
        if self.js_dialogs:
            result["browser_dialogs_accepted"], self.js_dialogs = self.js_dialogs, []
        if self.js_dialogs_dismissed:
            result["browser_dialogs_dismissed"], self.js_dialogs_dismissed = self.js_dialogs_dismissed, []
        return result

    def _settle_and_state(self, **kwargs: Any) -> dict[str, Any]:
        self._ensure()
        return self._state(idle=self._wait_idle(), **kwargs)

    def _after_step(self, before: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
        idle = self._wait_idle()
        state = self._state(idle=idle)
        seen = {(m["severity"], m["text"]) for m in before}
        new = [m for m in state.get("messages", []) if (m["severity"], m["text"]) not in seen]
        if new:  # each message once: messages keeps the older ones still shown
            state["new_messages"] = new
            state["messages"] = [m for m in state["messages"] if (m["severity"], m["text"]) in seen]
        state.update(extra)
        if self.dismissed:
            state["dismissed"], self.dismissed = list(self.dismissed), []
        return state

    def _context(self) -> dict[str, Any]:
        """The page context a press or row selection may move (see context_moves), with the address."""
        found = self._js("context")
        if not isinstance(found, dict):
            return {"error": "unreadable"}
        return dict(found, url=self._ensure().url())

    def _report_moves(self, state: dict[str, Any], before: dict[str, Any]) -> None:
        """`changed` lists what moved since `before`; `unchanged: true` when nothing did (with a note).
        Not said while D365 is still busy, nor when a context could not be read."""
        if state.get("idle") is False:
            return
        after = self._context()
        moved = context_moves(before, after)
        if moved is None:
            return
        if before.get("url") != after.get("url"):
            moved.append("address")
        if state.get("new_messages"):
            moved.append("messages")
        if state.get("browser_dialogs_accepted") or state.get("browser_dialogs_dismissed"):
            moved.append("browser_dialog")
        if moved:
            state["changed"] = moved
        else:
            state["unchanged"] = True
            state["note"] = UNCHANGED_NOTE

    def _locate(self, name: str, nth: int, kind: str, explicit: bool = False) -> dict[str, Any]:
        found = self._js("locate", name, nth, kind, explicit)
        if found.get("error"):
            details = {k: v for k, v in found.items() if k != "error" and v not in (None, [], 0)}
            if found.get("reason") == "tab_header_unavailable":
                raise D365WebError("refused", f"no unique visible header for tab {name!r}; nothing was clicked", **details)
            hint = ""
            if found.get("hidden_matches"):
                hint = " (it exists but is hidden: open its action tab or FastTab first)"
            raise D365WebError("not_found", f"no visible {'field' if kind == 'field' else 'control'} {name!r}"
                               f"{f' #{nth}' if nth else ''} in the current {found.get('scope', 'page')}{hint}", **details)
        if found.get("covered"):
            found = self._uncover(name, nth, kind, found, explicit)
        return found

    @staticmethod
    def _refuse_upload(found: dict[str, Any], name: str, applied: list[dict[str, Any]] | None = None) -> None:
        """A file upload control is not driven: its Browse opens the operating system's file picker, and which
        files of the server machine may be sent to D365 is not d365web's decision."""
        if found.get("upload"):
            raise D365WebError("refused", f"{name!r} belongs to the file upload control {found['upload']!r}: d365web "
                               "does not upload files (Browse opens the operating system's file picker); nothing "
                               "was pressed. Cancel the dialog, or upload the file by hand", upload=found["upload"],
                               **({"applied": list(applied)} if applied is not None else {}))

    def _model_may_act(self, found: dict[str, Any]) -> bool:
        """The model backend acts on this control: not on a grid row other than the active one, whose
        viewmodel command D365 would apply to the active record (the input path clicks that row first)."""
        return self.config.backend == "model" and not (found.get("in_row") and not found.get("active_row"))

    def _uncover(self, name: str, nth: int, kind: str, found: dict[str, Any], explicit: bool = False) -> dict[str, Any]:
        """A click at the control's centre would land on something lying over it. D365's "What's new"
        teaching tips (qtip, e.g. "Save queries to these views" after a filter changed the view) are
        acknowledged with their own button, as a person would, and noted in the result (`dismissed`);
        anything else is refused: nothing is clicked blindly."""
        cover = found["covered"]
        if cover.get("tip"):
            button = self._js("tipButton", cover["tip"])
            if not button.get("error"):
                page = self._ensure()
                page.click(button["x"], button["y"])
                self._wait_idle()
                self.dismissed.append(f"{button.get('text', '')} ({button.get('label', '')})".strip())
            found = self._js("locate", name, nth, kind, explicit)
            if found.get("error"):
                raise D365WebError("not_found", f"{name!r} is gone after dismissing a tip", **found)
            if not found.get("covered"):
                return found
            cover = found["covered"]
        raise D365WebError("covered", f"{name!r} is covered by {cover.get('by')!r}"
                           f"{(': ' + cover['text']) if cover.get('text') else ''}; nothing was clicked",
                           covered=cover)

    # --- operations (worker thread only) ----------------------------------------------------------
    def _op_open(self, menu_item: str, company: str | None, kind: str | None, guard: bool = True) -> dict[str, Any]:
        page = self._ensure()
        mi = f"{kind}:{menu_item}" if kind and kind != "display" else menu_item
        if not MENU_ITEM.fullmatch(mi):
            raise D365WebError("refused", f"bad menu item {mi!r}")
        cmp = (company or self.config.company).lower()
        if cmp and not re.match(r"^[a-z0-9]{1,8}$", cmp):
            raise D365WebError("refused", f"bad company {cmp!r}")
        query = f"cmp={quote(cmp)}&mi={quote(mi)}" if cmp else f"mi={quote(mi)}"  # no cmp: the user's default
        unsaved = d365web_model.unsaved_forms(self._js, strict=True) if guard else []
        if unsaved is None:
            raise D365WebError("refused", "cannot tell whether the page holds unsaved changes (D365 did not "
                               "answer); nothing was opened: check with web_state, then try again")
        if unsaved:
            # Navigating away drops them without D365 asking (live: two sales lines lost, 2026-11-06).
            raise D365WebError("refused", f"unsaved changes in {', '.join(unsaved)}: save them (web_press "
                               "SystemDefinedSaveButton) or leave the form with web_press SystemDefinedCloseButton "
                               "(D365 then saves valid changes and asks about the rest) before opening another page",
                               unsaved=unsaved)
        page.navigate(f"{self.config.url}/?{query}")
        time.sleep(0.5)
        return self._after_step([])

    def _op_set(self, values: list[tuple[str, Any]]) -> dict[str, Any]:
        """Set fields in order: each through the model where it can act, else by input; read back;
        stop at a rejection or a dialog the field opened."""
        page = self._ensure()
        before = self._js("messages")
        start = self._js("scope")
        applied: list[dict[str, Any]] = []
        for key, raw in values:
            name, nth = parse_field_key(key)
            explicit = field_key_explicit(key)
            value = "" if raw is None else str(raw)
            found = self._writable(name, nth, explicit, applied)
            via, raw_value = self._model_set(found, name, value, applied)
            if via == "input":
                self._input_set(page, found, name, (nth, explicit), value, applied)
            self._wait_idle()
            applied.append(self._readback(key, name, (nth, explicit), found, value, via, raw_value))
            if self._set_stops(before, start):
                return self._after_step(before, applied=applied, stopped_at=key)
        return self._after_step(before, applied=applied)

    def _writable(self, name: str, nth: int, explicit: bool, applied: list[dict[str, Any]]) -> dict[str, Any]:
        """The field, once it is writable: an earlier field may enable it (READONLY_GRACE)."""
        found = self._locate(name, nth, "field", explicit)
        self._refuse_upload(found, name, applied)
        if found.get("role") == "ReferenceGroup":
            parts = found.get("parts") or []
            raise D365WebError("refused", f"{name!r} is a reference group: set its field "
                               f"{' or '.join(repr(p) for p in parts) or '(see web_state fields)'} instead; nothing was "
                               "typed (a click on the group can open the referenced record)", parts=parts,
                               applied=list(applied))
        deadline = time.monotonic() + READONLY_GRACE
        while found.get("readonly") and time.monotonic() < deadline:  # enabled by an earlier field?
            time.sleep(0.25)
            found = self._locate(name, nth, "field", explicit)
        if found.get("readonly"):
            hint = " (a grid line other than the current one: select that line first, web_select_row)" \
                if found.get("in_row") and not found.get("active_row") else ""
            raise D365WebError("refused", f"field {name!r} is read-only here{hint}", applied=list(applied))
        return found

    def _model_set(self, found: dict[str, Any], name: str, value: str,
                   applied: list[dict[str, Any]]) -> tuple[str, bool]:
        """(via, raw_value): "model" when the model acted (raw_value: compare D365's own value), else
        "input" for the input path. A combo without the option is refused here, never typed."""
        if not self._model_may_act(found):
            return "input", False
        if found.get("role") == "ComboBox" and value.strip().lower() == str(found.get("value", "")).strip().lower():
            return "model", False  # nothing to send: a repeated SetSelectedItem would rerun D365's modified()
        acted = d365web_model.set_value(self._js, found, value, as_bool(value) if found.get("check") else None)
        if acted is None:
            return "input", False
        if acted.get("error") == "no_option":
            raise D365WebError("refused", f"{name!r} has no option {value!r}",
                               options=acted.get("options", []), applied=list(applied))
        return "model", bool(acted.get("raw"))

    def _input_set(self, page: Page, found: dict[str, Any], name: str, where: tuple[int, bool], value: str,
                   applied: list[dict[str, Any]]) -> None:
        """The input path: a checkbox is clicked (twice at most), a combo picked from its list, any other
        field clicked, selected, typed and left with Tab."""
        nth, explicit = where
        if found.get("check"):
            if as_bool(value) != (found.get("value") == "Yes"):
                page.click(found["x"], found["y"])
                self._wait_idle()
                again = self._js("locate", name, nth, "field", explicit)
                if not again.get("error") and (again.get("value") == "Yes") != as_bool(value):
                    # the click was swallowed (a re-query, a popup): once more, through _locate's checks
                    again = self._locate(name, nth, "field", explicit)
                    if (again.get("value") == "Yes") != as_bool(value):
                        page.click(again["x"], again["y"])
            return
        if found.get("role") == "ComboBox":  # an enum: pick from its list, typing is ignored
            if value.strip().lower() != str(found.get("value", "")).strip().lower():
                self._pick_combo(page, found, name, value, applied)
            return
        if found.get("role") != "MultilineInput" and any(c in value for c in "\r\n\t"):
            # typed, a line break or tab is a key: Enter presses the default button, Tab moves on
            raise D365WebError("refused", f"{name!r} is a one-line field: the value holds a line "
                               "break or tab; nothing was typed", applied=list(applied))
        page.click(found["x"], found["y"])
        time.sleep(0.15)
        self._require_focus(found, name, applied, where)
        page.key("Ctrl+a")
        if value:
            page.type_text(value)
        else:
            page.key("Delete")
        page.key("Tab")

    def _readback(self, key: str, name: str, where: tuple[int, bool], found: dict[str, Any], value: str,
                  via: str, raw_value: bool) -> dict[str, Any]:
        """The `applied` line: what the field holds now, `differs` when it did not take the value,
        a password confirmed through D365's own control state (never read back as text)."""
        nth, explicit = where
        now = self._js("locate", name, nth, "field", explicit)
        at = now if "x" in now and "y" in now else found  # the written control, for D365's own value
        hidden = secret(name) or secret(found.get("control", ""), found.get("password", False))
        line = {"field": key, "value": MASK if hidden else value}
        if self.config.backend == "model":
            line["via"] = via  # which backend acted; input = the model could not act on this control
        if hidden and via == "model":
            # A password's input reads back empty; D365's own control knows whether it holds a value.
            state = self._js("model:secretState", at["x"], at["y"], found.get("control", name))
            if isinstance(state, dict) and "empty" in state:
                if state["empty"] == bool(value):
                    line["differs"] = True  # the secret did not take (or a clear did not)
                else:
                    line["confirmed"] = True
        if now.get("error"):
            return line
        line["now"] = MASK if hidden else now.get("value", "")
        # A password placeholder or empty readback cannot confirm the value.
        if not hidden and found.get("check"):
            if (line["now"] == "Yes") != as_bool(value):
                line["differs"] = True  # the checkbox did not take the value, even after a retry
        elif not hidden and value_differs(value, line["now"]):
            # An ISO date set through the model shows in the user's format: compare D365's own value.
            raw = self._js("model:rawValue", at["x"], at["y"], found.get("control", name)) if raw_value else None
            if not (isinstance(raw, dict) and iso_matches(value, str(raw.get("value", "")))):
                line["differs"] = True  # D365 changed or silently dropped the value
        return line

    def _set_stops(self, before: list[dict[str, Any]], start: Any) -> bool:
        """Stop after this field: D365 rejected a value (as a warning: "... is not found in the related
        table"), or the field opened a dialog (D365 asks before applying a customer group's dimensions),
        which the caller answers before the remaining fields."""
        if [m for m in self._js("messages") if m["severity"] in ("error", "warning") and m not in before]:
            return True
        scope = self._js("scope")
        return isinstance(scope, dict) and isinstance(start, dict) and scope.get("scope") == "dialog" and \
            (scope.get("scope"), scope.get("form")) != (start.get("scope"), start.get("form"))

    def _require_focus(self, found: dict[str, Any], name: str, applied: list[dict[str, Any]],
                       where: tuple[int, bool] = (0, False)) -> None:
        """Keys go wherever the focus is: type only into the clicked field (a grid row change, a popup
        that came up after the click can take it elsewhere)."""
        if self._js("focusedAt", found["x"], found["y"]) is not False:  # True, or a page that cannot say
            return
        self._wait_idle()
        # the click may have scrolled the grid or form: look at the field where it is now
        again = self._js("locate", name, where[0], "field", where[1])
        at = again if isinstance(again, dict) and "x" in again and "y" in again else found
        if self._js("focusedAt", at["x"], at["y"]) is not False:
            return
        raise D365WebError("refused", f"{name!r} did not take the keyboard focus after the click; nothing was "
                           "typed (check web_state for a dialog or the active line)", applied=list(applied))

    def _wait_list(self, kind: str) -> bool:
        """Let a click open its list before considering a keyboard toggle."""
        for _ in range(4):
            if self._js("listOpen", kind) is True:
                return True
            time.sleep(0.05)
        return self._js("listOpen", kind) is True

    def _dismiss_list(self, page: Page, kind: str) -> bool:
        # Escape on a form can mean Back; only send it to an observed open list.
        if self._js("listOpen", kind) is not True:
            return False
        page.key("Escape")
        return True

    def _pick_combo(self, page: Page, found: dict[str, Any], name: str, value: str,
                    applied: list[dict[str, Any]]) -> None:
        page.click(found["x"], found["y"])
        time.sleep(0.15)
        if not self._wait_list("combo"):
            page.key("Alt+ArrowDown")
        option: dict[str, Any] = {}
        for _ in range(12):
            time.sleep(0.25)
            option = self._js("comboOption", value)
            if not option.get("error") or option.get("options"):
                break
        if option.get("error"):
            self._dismiss_list(page, "combo")
            if option["error"] == "covered":  # scrolled to, but something else lies over it: click nothing
                raise D365WebError("refused", f"{name!r}: option {value!r} is covered in its list",
                                   applied=list(applied))
            raise D365WebError("refused", f"{name!r} has no option {value!r}",
                               options=option.get("options", []), applied=list(applied))
        page.click(option["x"], option["y"])

    def _op_press(self, name: str, nth: int) -> dict[str, Any]:
        page = self._ensure()
        before = self._js("messages")
        found = self._locate(name, nth, "any")
        self._refuse_upload(found, name)
        if found.get("enabled") is False:
            raise D365WebError("refused", f"{name!r} is disabled")
        extra: dict[str, Any] = {}
        context = self._context()  # dispatch and idle are not proof that anything happened: compare after
        acted = d365web_model.press(self._js, found) if self._model_may_act(found) else None
        if acted is None:
            page.click(found["x"], found["y"])
            time.sleep(0.2)
        if self.config.backend == "model":
            extra["via"] = "model" if acted is not None else "input"
        state = self._after_step(before, pressed=found.get("control", name), **extra)
        self._report_moves(state, context)
        return state

    def _op_key(self, keys: str) -> dict[str, Any]:
        page = self._ensure()
        before = self._js("messages")
        for combo in keys.split():
            page.key(combo)
            time.sleep(0.1)
        return self._after_step(before)

    def _op_lookup(self, name: str, given: int | None, pick: str | None) -> dict[str, Any]:
        page = self._ensure()
        before = self._js("messages")
        nth, explicit = given or 0, given is not None
        model = self.config.backend == "model"
        if model and self._js("scope")["scope"] != "lookup":
            found = self._locate(name, nth, "field", explicit)
            model = self._model_may_act(found)
            if model and d365web_model.open_lookup(self._js, found):
                self._wait_idle()
        if model:
            if self._js("scope")["scope"] == "lookup":
                if pick is None:
                    return self._after_step(before, via="model")
                deadline = time.monotonic() + self.config.step_timeout
                found = d365web_model.pick_lookup_row(self._js, pick, self._wait_idle, GRID_SCAN_CAP,
                                                      lambda: time.monotonic() < deadline)
                if found.get("ambiguous"):
                    self._refuse_ambiguous_pick(page, pick, {"matches": found["ambiguous"]})
                if found.get("picked"):
                    self._wait_idle()  # the field shows the chosen row once D365 is done
                    return self._after_step(before, picked=pick, via="model", **self._lookup_readback(name, nth, explicit, found))
                shown = self._js("row", "", 0, {"*": pick}) if found.get("complete") else {}
                if shown.get("error") == "ambiguous":
                    self._refuse_ambiguous_pick(page, pick, shown)
                if found.get("complete") and shown.get("error"):
                    # Every row searched, by value and by shown text: refuse without typing into the field.
                    rows = self._js("grid", "", 10)
                    if d365web_model.cancel_lookup(self._js):
                        self._wait_idle()
                    raise D365WebError("not_found", f"no lookup row with a value {pick!r}", lookup=rows)
            # Not opened, or the value may be a shown text only: the input path below (it types to filter).
        if self._js("scope")["scope"] != "lookup":
            self._open_lookup(page, name, nth, None, explicit)
        if pick is None:
            return self._after_step(before)
        target = self._js("row", "", 0, {"*": pick})
        if target.get("error") == "ambiguous":
            self._refuse_ambiguous_pick(page, pick, target)
        if target.get("error"):
            self._dismiss_list(page, "lookup")
            self._wait_idle()
            self._open_lookup(page, name, nth, pick, explicit)  # typed text filters the lookup
            target = self._js("row", "", 0, {"*": pick})
            if target.get("error") == "ambiguous":
                self._refuse_ambiguous_pick(page, pick, target, (name, nth, explicit))
            if target.get("error"):
                rows = self._js("grid", "", 10)
                self._dismiss_list(page, "lookup")
                self._wait_idle()
                # The text typed to filter the lookup stays in the field (D365 validates it): say what it holds.
                now = self._js("locate", name, nth, "field", explicit)
                shown = now.get("value") if isinstance(now, dict) and not now.get("error") else None
                raise D365WebError("not_found", f"no lookup row with a value {pick!r}" + (
                    f"; the field now holds {shown!r} (the filter text typed into it): check or set it" if shown else ""),
                    lookup=rows, **({"now": shown} if shown is not None else {}))
        page.click(target["x"], target["y"])
        self._wait_idle()
        now = self._js("locate", name, nth, "field", explicit)  # what the field holds after the pick
        shown = {"now": now.get("value", "")} if isinstance(now, dict) and not now.get("error") \
            and not now.get("password") else {}
        return self._after_step(before, picked=pick, **shown)

    def _refuse_ambiguous_pick(self, page: Page, pick: str, target: dict[str, Any],
                               field: tuple[str, int, bool] | None = None) -> None:
        rows = self._js("grid", "", 10)
        if not d365web_model.cancel_lookup(self._js):
            self._dismiss_list(page, "lookup")
        self._wait_idle()
        now: dict[str, Any] = {}
        if field is not None:  # the filter text was typed into the field: it stays there, D365 validates it
            found = self._js("locate", *field[:2], "field", field[2])
            if isinstance(found, dict) and not found.get("error") and not found.get("password"):
                now = {"now": found.get("value", "")}
        raise D365WebError("refused", f"{target.get('matches', 'several')} lookup rows hold {pick!r}; nothing was "
                           "picked: give a value only one row holds (the key column)" +
                           (f"; the field now holds {now['now']!r} (the filter text): check or set it" if now else ""),
                           lookup=rows, matches=target.get("matches"), **now)

    def _lookup_readback(self, name: str, nth: int, explicit: bool, picked: dict[str, Any]) -> dict[str, Any]:
        """The field after a model pick: `now`, and `differs` when it holds none of the picked row's values."""
        now = self._js("locate", name, nth, "field", explicit)
        if not isinstance(now, dict) or now.get("error") or now.get("password"):
            return {}
        out: dict[str, Any] = {"now": now.get("value", "")}
        values = [v for v in picked.get("values") or [] if v.strip()]
        if values and all(value_differs(v, str(out["now"])) for v in values):
            out["differs"] = True  # D365 shows something else than the chosen row: check the field
        return out

    def _open_lookup(self, page: Page, name: str, nth: int, typed: str | None, explicit: bool = False) -> None:
        found = self._locate(name, nth, "field", explicit)
        page.click(found["x"], found["y"])
        time.sleep(0.15)
        if typed is not None:
            # A click may already have opened it; return focus to the field before filtering.
            if self._dismiss_list(page, "lookup"):
                self._wait_idle()
            page.key("Ctrl+a")
            page.type_text(typed)
        if not self._wait_list("lookup"):
            page.key("Alt+ArrowDown")
        time.sleep(0.2)
        self._wait_idle()
        if self._js("scope")["scope"] != "lookup":
            raise D365WebError("no_lookup", f"{name!r} did not open a lookup (Alt+Down)")

    def _op_scenario(self, steps: list[dict[str, Any]], variables: dict[str, Any] | None,
                     stop_on_mismatch: bool, full_final: bool = False) -> dict[str, Any]:
        values = {k: str(v) for k, v in (variables or {}).items()}
        transcript: list[dict[str, Any]] = []
        state: dict[str, Any] | None = None
        ok = True
        for index, step in enumerate(steps, 1):
            op = step["op"]
            line: dict[str, Any] = {"step": index, "op": op}
            if step.get("note"):
                line["note"] = step["note"]
            try:
                args = substitute({k: v for k, v in step.items() if k in STEP_ARGS[op]}, values)
                expect = substitute(step.get("expect") or {}, values)
                _check_web_expect(expect)
                state = self._dispatch(op, args)
            except D365WebError as error:
                line.update(error=error.code, message=error.message)
                if error.details:
                    line["details"] = error.details
                transcript.append(line)
                ok = False
                break
            line["at"] = f"{state.get('scope')}:{state.get('form')}"
            if state.get("caption"):
                line["caption"] = state["caption"]
            if state.get("new_messages"):
                shown = state["new_messages"][:5]
                line["messages"] = [f"{m['severity']}: {m['text'][:300]}" for m in shown]
                if len(state["new_messages"]) > len(shown):
                    line["messages"].append(f"(+{len(state['new_messages']) - len(shown)} more)")
            if state.get("pressed"):
                line["pressed"] = state["pressed"]
            for key in ("unchanged", "changed", "active_differs"):
                if state.get(key):
                    line[key] = state[key]
            if state.get("dismissed"):
                line["dismissed"] = state["dismissed"]
            if [a for a in state.get("applied", []) if a.get("differs")]:
                line["differs"] = [a["field"] for a in state["applied"] if a.get("differs")]
            checked = state
            if step.get("expect") or step.get("save"):  # labels of every control, not only the compact ones
                checked = dict(state, labels={**self._js("labels"), **(state.get("labels") or {})})
            misses = check_expectation(checked, expect)
            misses = self._grid_misses_beyond_screen(misses, expect)
            for name, spec in (step.get("save") or {}).items():
                value = extract_variable(checked, spec)
                if value is None:
                    misses.append(f"save {name}: nothing found for {spec}")
                else:
                    values[name] = value
                    line.setdefault("saved", {})[name] = value
            if misses:
                line["mismatch"] = misses
                ok = False
            transcript.append(line)
            if misses and stop_on_mismatch:
                break
        result: dict[str, Any] = {"ok": ok, "steps": transcript, "variables": values}
        if state is not None:  # the whole page on request; web_state shows it later as well
            result["final"] = state if full_final else {
                k: state[k] for k in ("scope", "form", "caption", "messages", "missing", "url") if state.get(k)}
        return result

    def _dispatch(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        check_operation(op, args)
        if op == "open":
            return self._op_open(args["menu_item"], args.get("company"), args.get("kind"))
        if op == "state":
            return self._state(full=bool(args.get("full")), max_rows=int(args.get("max_rows", 20)))
        if op == "set":
            return self._op_set(normalize_values(args["values"]))
        if op == "press":
            return self._op_press(args["name"], int(args.get("nth", 0)))
        if op == "lookup":
            return self._op_lookup(args["field"], int(args["nth"]) if "nth" in args else None, args.get("pick"))
        if op == "filter":
            return self._op_filter(str(args.get("value", "")), args.get("column"))
        if op == "select_row":
            return self._op_select_row(args.get("grid", ""), int(args.get("row", 0)), args.get("match"))
        if op == "key":
            return self._op_key(args["keys"])
        if op == "tree":
            return self._op_tree(args.get("tree") or "", list(args["path"]))
        raise D365WebError("refused", f"unknown op {op!r}")

    def _op_filter(self, value: str, column: str | None) -> dict[str, Any]:
        page = self._ensure()
        before = self._js("messages")
        if self.config.backend == "model":
            sc = self._js("scope")
            form = sc.get("form", "") if isinstance(sc, dict) and sc.get("scope") != "page" else ""
            done = d365web_model.filter_grid(self._js, value, column, form)  # a column name or its label
            if done and done.get("error") == "no_column":
                raise D365WebError("not_found", f"the quick filter offers no column {column!r}", columns=done.get("columns", []))
            if done:
                info = {"cleared": True} if done.get("cleared") else {"column": done.get("column", column), "value": value}
                return self._after_step(before, filter=info, via="model")
        box = self._js("quickfilter")
        if box.get("error"):
            raise D365WebError("not_found", "no quick filter in the current scope",
                               **{k: v for k, v in box.items() if k != "error"})
        page.click(box["x"], box["y"])
        time.sleep(0.15)
        page.key("Ctrl+a")
        if not value:
            page.key("Delete")
            page.key("Enter")
            return self._after_step(before, filter={"cleared": True})
        page.type_text(value)
        option: dict[str, Any] = {}
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            time.sleep(0.25)
            option = self._js("filteroption", self._column_label(column))
            if not option.get("error") or option.get("columns"):
                break
        if option.get("error"):
            self._dismiss_list(page, "filter")
            raise D365WebError("not_found", f"the quick filter offers no column {column!r}",
                               columns=option.get("columns", []))
        page.click(option["x"], option["y"])
        return self._after_step(before, filter={"column": option["column"], "value": value})

    def _column_label(self, column: str | None) -> str:
        """A grid column name (WHSWorkTable_OrderNum) to its header label (Order number); labels pass."""
        if not column:
            return ""
        grid = self._js("grid", "", 1)
        for col in grid.get("columns", []) if isinstance(grid, dict) else []:
            if col.get("name") == column and col.get("label"):
                return col["label"]
        return column

    def _grid_misses_beyond_screen(self, misses: list[str], expect: dict[str, Any]) -> list[str]:
        """An expected grid row that is not among the rendered rows is searched in the whole grid
        (scrolling, up to GRID_SCAN_CAP rows) before it counts as missing."""
        spec = expect.get("grid") or {}
        contains = spec.get("contains")
        miss = f"grid: no row with {contains}"
        if not contains or miss not in misses:
            return misses
        scan = self._scan_grid(spec.get("name") or "",
                               stop=lambda rows, columns: any(_row_matches(columns, r, contains) for r in rows))
        if any(_row_matches(scan["columns"], r, contains) for r in scan["rows"]):
            return [m for m in misses if m != miss]
        detail = f"searched {len(scan['rows'])} rows" + (", stopped at the cap" if scan["truncated"] else "")
        return [f"{m} ({detail})" if m == miss else m for m in misses]

    def _scan_grid(self, grid: str, stop: Any = None) -> dict[str, Any]:
        """Scroll a grid from the top like a person, collecting rows (by aria-rowindex, else by their
        values) until the end, GRID_SCAN_CAP rows, or stop(rows, columns) is true. complete: the end was reached."""
        first = self._js("grid", grid, 200)
        if first.get("error"):
            raise D365WebError("not_found", f"no visible grid {grid!r}", **{k: v for k, v in first.items() if k != "error"})
        rows: dict[str, dict[str, Any]] = {}

        def add(result: dict[str, Any]) -> int:
            before = len(rows)
            for row in result.get("rows", []):
                key = str(row.get("key") or json.dumps(row.get("values")))
                if key not in rows and len(rows) < GRID_SCAN_CAP:
                    rows[key] = row
            return len(rows) - before

        top = self._js("gridScroll", grid, "top")
        if top.get("scrollable"):
            self._wait_idle()
            first = self._js("grid", grid, 200)
        add(first)
        columns = first.get("columns", [])
        stopped = lambda: bool(stop and stop(list(rows.values()), columns))  # noqa: E731
        complete = not top.get("scrollable") or self._scroll_collect(grid, add, rows, stopped)
        hint = first.get("rowcount_hint") or 0  # aria-rowcount, the header row included
        moved = False
        if complete and hint and len(rows) < hint - 1 and not stopped():
            if self.config.backend == "model":
                complete, hint = self._load_by_model(grid, first.get("name") or grid, add, rows, stopped, hint)
            else:
                complete, moved = self._load_by_keyboard(grid, add, rows, stopped, hint)
        listed = [dict(row, row=i) for i, row in enumerate(rows.values())]
        result = {"name": first.get("name"), "columns": first.get("columns", []), "rows": listed,
                  "complete": complete, "truncated": len(rows) >= GRID_SCAN_CAP and not complete}
        if moved:
            result["selection_moved"] = True  # the input backend clicked the last row and paged down to load more
        return result

    def _scroll_collect(self, grid: str, add: Callable[[dict[str, Any]], int], rows: dict[str, Any],
                        stopped: Callable[[], bool]) -> bool:
        """Scroll down, collecting, until the bottom (no scroll and no new rows twice: D365 may still be
        loading the next block), the cap or stop. True: the bottom was reached."""
        still = 0
        while len(rows) < GRID_SCAN_CAP and not stopped():
            step = self._js("gridScroll", grid, "down")
            self._wait_idle()
            gained = add(self._js("grid", grid, 200))
            if step.get("after", 0) <= step.get("before", 0) and not gained:
                still += 1
                if still >= 2:
                    return True
            else:
                still = 0
        return False

    def _load_by_model(self, grid: str, name: str, add: Callable[[dict[str, Any]], int], rows: dict[str, Any],
                       stopped: Callable[[], bool], hint: int) -> tuple[bool, int]:
        """Model backend: a read never sends input. Ask the grid's viewmodel for its remaining blocks
        (LoadNextPage, only the scope's grid), then scroll again; no selection click, no PageDown."""
        deadline = time.monotonic() + self.config.step_timeout
        sc = self._js("scope")
        form = sc.get("form", "") if isinstance(sc, dict) and sc.get("scope") != "page" else ""
        pages = d365web_model.load_all_pages(self._js, name, self._wait_idle, GRID_SCAN_CAP,
                                             lambda: time.monotonic() < deadline, form)
        complete = True
        if pages and pages.get("pages"):
            self._js("gridScroll", grid, "top")
            self._wait_idle()
            complete = self._scroll_collect(grid, add, rows, stopped)
            hint = self._js("grid", grid, 200).get("rowcount_hint") or hint
        return complete and not (hint and len(rows) < hint - 1), hint

    def _load_by_keyboard(self, grid: str, add: Callable[[dict[str, Any]], int], rows: dict[str, Any],
                          stopped: Callable[[], bool], hint: int) -> tuple[bool, bool]:
        """Input backend: some grids keep one data block in the DOM, all of it in view, and fetch the next
        block only when the selection moves past its last row with the keyboard (a custom receiving log
        on the test box, 2026-10-26: 30 of 43 rows). Select the last rendered row, then page down like a
        person. This moves the grid's selection, which a scroll does not. (complete, selection moved)"""
        last = self._js("row", grid, max(0, len(self._js("grid", grid, 200).get("rows", [])) - 1), None)
        if last.get("error"):
            return True, False
        page = self._ensure()
        page.click(last["x"], last["y"])
        rest = self._js("restPoint")
        if not rest.get("error"):
            page.move(rest["x"], rest["y"])
        self._wait_idle()
        still = 0
        while not stopped():  # also after the last block came
            if len(rows) >= min(hint - 1, GRID_SCAN_CAP):
                break
            page.key("PageDown")
            time.sleep(0.2)
            self._wait_idle()
            if add(self._js("grid", grid, 200)):
                still = 0
            else:
                still += 1  # the next block may still be loading: look again before giving up
                if still >= 3:
                    break
        return len(rows) >= hint - 1, True

    def _op_select_row(self, grid: str, row: int, match: dict[str, Any] | None) -> dict[str, Any]:
        page = self._ensure()
        before = self._js("messages")
        target = self._js("row", grid, row, match)
        searched = None
        if target.get("error") == "not_found" and target.get("what") == "row" and match:
            found: dict[str, Any] = {}

            def hit(rows: list[dict[str, Any]], columns: list[dict[str, Any]]) -> bool:  # rendered now
                result = self._js("row", grid, 0, match)
                if not result.get("error") or result.get("error") in ("links_only", "ambiguous"):
                    found.update(result)
                return bool(found)
            scan = self._scan_grid(grid, stop=hit)
            searched = len(scan["rows"])
            if found:
                target = found
            else:
                target = dict(target, searched_rows=searched, truncated=scan["truncated"])
        if target.get("error") == "ambiguous":
            raise D365WebError("refused", f"{target.get('matches')} rows of grid {target.get('grid')!r} match; "
                               "nothing was selected: narrow the match", **{k: v for k, v in target.items() if k != "error"})
        if target.get("error") == "links_only":
            raise D365WebError("refused", f"row {target['row']} of grid {target['grid']!r} has only hyperlink cells: "
                               "a click would open the record, not select the row; filter the list to it instead",
                               **{k: v for k, v in target.items() if k != "error"})
        if target.get("error"):
            raise D365WebError("not_found", f"no {'matching ' if match else ''}row in grid {grid or '(largest)'!r}"
                               + (f" (searched {searched} rows, scrolling)" if searched is not None else ""),
                               **{k: v for k, v in target.items() if k != "error"})
        context = self._context()
        page.click(target["x"], target["y"])
        # the pointer leaves the row for an inert spot of the form: resting on a linked record opens D365's
        # hover preview over the form (test box, 2026-10-26: the setup form's menu list); the page corner is
        # no good either, its Office 365 launcher shows a tooltip over the action pane
        rest = self._js("restPoint")
        if not rest.get("error"):
            page.move(rest["x"], rest["y"])
        state = self._after_step(before, selected={"grid": target["grid"], "row": target["row"]})
        self._report_active(state, target)
        self._report_moves(state, context)
        if state.get("unchanged") and not state.get("active_differs") and state.get("active", {}).get("key"):
            state["note"] = "the row was already the active record: nothing moved"
        return state

    def _report_active(self, state: dict[str, Any], target: dict[str, Any]) -> None:
        """After a row click: the record D365 calls active (the one the detail fields and commands work
        on), and `active_differs` when that is not the clicked row (the click was swallowed, a validation
        kept the old row). Nothing is said when the grid has no viewmodel."""
        active = self._js("active", target["grid"])
        if not isinstance(active, dict) or active.get("error") or active.get("unknown"):
            return
        state["active"] = {k: active[k] for k in ("grid", "row", "key", "values") if k in active}
        if state.get("caption"):
            state["active"]["caption"] = state["caption"]
        if target.get("key") and active.get("key") and str(target["key"]) != str(active["key"]):
            state["active_differs"] = True

    def _op_tree(self, tree: str, path: list[str]) -> dict[str, Any]:
        """Select a tree node by its label path (root first): each parent is expanded (D365 loads its
        children), then the last node selected, by clicks on the node's expand button and label. Labels are
        matched exactly (case and outer spaces aside); a miss or two siblings with the label is refused
        with the labels on offer. Tree checkboxes are never touched."""
        page = self._ensure()
        before = self._js("messages")
        context = self._context()
        parent: str | None = None
        acted = False
        for depth, label in enumerate(path):
            node = self._tree_node(tree, parent, label, path[:depth])
            tree = node["tree"]
            last = depth == len(path) - 1
            action = "select" if last else "expand"
            if (last and node.get("selected")) or (not last and node.get("expanded") is not False):
                parent = node["id"]
                continue  # already selected, or expanded (or a leaf, which the next label then misses)
            if action == "expand" and "bx" not in node:
                raise D365WebError("refused", f"tree node {label!r} shows no expand button; nothing was clicked",
                                   tree=tree, path=path[:depth + 1])
            # Clicks, as a person: the tree's viewmodel commands take node objects the page does not hand
            # out (its node data is plain, live 10.0.49), so the model has nothing safe to call here.
            page.click(*((node["bx"], node["by"]) if action == "expand" else (node["x"], node["y"])))
            acted = True
            self._wait_idle()
            parent = node["id"]
        extra: dict[str, Any] = {"tree": {"name": tree, "selected": path}}
        if self.config.backend == "model" and acted:
            extra["via"] = "input"
        state = self._after_step(before, **extra)
        self._report_moves(state, context)
        return state

    def _tree_node(self, tree: str, parent: str | None, label: str, above: list[str]) -> dict[str, Any]:
        """The node, once rendered: children appear a moment after their parent was expanded."""
        deadline = time.monotonic() + READONLY_GRACE
        while True:
            node = self._js("treeNode", tree, parent, label)
            if not isinstance(node, dict):
                raise D365WebError("not_found", "the page did not answer for the tree")
            if not node.get("error"):
                return node
            if node.get("error") == "ambiguous" or node.get("what") == "tree" or time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        where = " under " + " / ".join(above) if above else ""
        details = {k: v for k, v in node.items() if k in ("tree", "trees", "options")}
        if node.get("what") == "tree":
            message = ("more than one tree here: name one (tree)" if node["error"] == "ambiguous"
                       else f"no visible tree {tree!r} in the current {node.get('scope', 'page')}")
            raise D365WebError("refused" if node["error"] == "ambiguous" else "not_found", message, **details)
        if node["error"] == "ambiguous":
            raise D365WebError("refused", f"more than one tree node {label!r}{where}; nothing was selected", **details)
        raise D365WebError("not_found", f"no tree node {label!r}{where}", **details)

    def _op_grid(self, grid: str, max_rows: int, start_row: int = 0, all_rows: bool = False) -> dict[str, Any]:
        self._ensure()
        if not all_rows and start_row == 0:
            result = self._js("grid", grid, max_rows)
            if result.get("error"):
                raise D365WebError("not_found", f"no visible grid {grid!r}", **{k: v for k, v in result.items() if k != "error"})
            return result
        want = None if all_rows else start_row + max_rows + 1
        scan = self._scan_grid(grid, stop=(lambda rows, columns: len(rows) >= want) if want else None)
        rows = scan["rows"] if all_rows else scan["rows"][start_row:start_row + max_rows]
        out = {"name": scan["name"], "columns": scan["columns"], "rows": rows, "start_row": 0 if all_rows else start_row,
               "more": (not scan["complete"]) if all_rows else (len(scan["rows"]) > start_row + max_rows or not scan["complete"]),
               "scrolled": True}
        if scan["truncated"]:
            out["truncated"] = True
            out["note"] = f"stopped at {GRID_SCAN_CAP} rows; filter the grid to narrow it"
        if scan.get("selection_moved"):
            out["selection_moved"] = True  # (input backend) the read selected the last row to load more
        return out

    def _op_click(self, x: float, y: float) -> dict[str, Any]:
        page = self._ensure()
        before = self._js("messages")
        page.click(x, y)
        return self._after_step(before)

    def _op_type(self, text: str) -> dict[str, Any]:
        page = self._ensure()
        before = self._js("messages")
        page.type_text(text)
        return self._after_step(before)

    def _op_screenshot(self) -> dict[str, Any]:
        page = self._ensure()
        folder = (self.state_dir or Path.cwd()) / "d365web-screens"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}.png")  # two in a second
        page.screenshot(path)
        return {"path": str(path), "bytes": path.stat().st_size}

    # --- public API (any thread) ------------------------------------------------------------------
    def open(self, menu_item: str, company: str | None = None, kind: str | None = None) -> dict[str, Any]:
        check_operation("open", {"menu_item": menu_item, "company": company, "kind": kind})
        return self.call("web_open", lambda: self._op_open(menu_item, company, kind))

    def state(self, full: bool = False, max_rows: int = 20) -> dict[str, Any]:
        check_operation("state", {"full": full, "max_rows": max_rows})
        return self.call("web_state", lambda: self._state(full=full, max_rows=max_rows))

    def set(self, values: dict[str, Any] | list[Any]) -> dict[str, Any]:
        """values: {name: value}, or [[name, value], ...] when the order matters (JSON objects that
        pass through other services may arrive with their keys sorted)."""
        check_operation("set", {"values": values})
        pairs = normalize_values(values)
        return self.call("web_set", lambda: self._op_set(pairs))

    def press(self, name: str, nth: int = 0) -> dict[str, Any]:
        check_operation("press", {"name": name, "nth": nth})
        return self.call("web_press", lambda: self._op_press(name, nth))

    def key(self, keys: str) -> dict[str, Any]:
        check_operation("key", {"keys": keys})
        return self.call("web_key", lambda: self._op_key(keys))

    def lookup(self, name: str, pick: str | None = None, nth: int | None = None) -> dict[str, Any]:
        check_operation("lookup", {"field": name, "pick": pick, **({} if nth is None else {"nth": nth})})
        return self.call("web_lookup", lambda: self._op_lookup(name, nth, pick))

    def select_row(self, grid: str = "", row: int = 0, match: dict[str, Any] | None = None) -> dict[str, Any]:
        check_operation("select_row", {"grid": grid, "row": row, "match": match})
        return self.call("web_select_row", lambda: self._op_select_row(grid, row, match))

    def tree(self, path: list[str], tree: str = "") -> dict[str, Any]:
        check_operation("tree", {"path": path, "tree": tree})
        return self.call("web_tree", lambda: self._op_tree(tree, list(path)))

    def run_scenario(self, steps: list[dict[str, Any]], variables: dict[str, Any] | None = None,
                     stop_on_mismatch: bool = True, full_final: bool = False) -> dict[str, Any]:
        check_steps(steps, variables)
        return self.call("web_run_scenario",
                         lambda: self._op_scenario(steps, variables, stop_on_mismatch, full_final))

    def filter(self, value: str, column: str | None = None) -> dict[str, Any]:
        check_operation("filter", {"value": value, "column": column})
        return self.call("web_filter", lambda: self._op_filter(value, column))

    def grid(self, grid: str = "", max_rows: int = 50, start_row: int = 0, all_rows: bool = False) -> dict[str, Any]:
        if start_row < 0:
            raise D365WebError("refused", "start_row must be 0 or more")
        return self.call("web_grid", lambda: self._op_grid(grid, max_rows, start_row, all_rows))

    def click(self, x: float, y: float) -> dict[str, Any]:
        return self.call("web_click", lambda: self._op_click(x, y))

    def type(self, text: str) -> dict[str, Any]:
        return self.call("web_type", lambda: self._op_type(text))

    def screenshot(self) -> dict[str, Any]:
        return self.call("web_screenshot", self._op_screenshot)

    def close_browser(self) -> dict[str, Any]:
        return self.call("web_close", self._close_browser)
