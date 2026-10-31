"""boring-pondnt-d365web: test-drive the D365 Finance and Operations web client through a real Edge.

    boring-pondnt-d365web serve --config <service.json> --d365web <d365web.json> [--state-dir <dir>]

d365web.json: {"url": "https://<aos host>", "company": "<optional default company>", "profile": "alligator", "debug_port": 9333,
"headless": true, "idle_minutes": 30, "step_timeout": 90}. The profile is a persistent Edge profile that a
person signed in to once (docs/D365WEB.md); the server never sees a password. One browser, one tab, one
step at a time: parallel callers queue behind each other (busy).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__
from .d365web_client import Config, D365WebError, StillRunning, WebClient
from .tool_schema import AnyMap, NameValuePairs
from boring_service_kit.access import permitted
from boring_service_kit.calllog import CallLog
from boring_service_kit.http import ServiceConfig, add_config_argument
from boring_service_kit.outcomes import Failed, Outcome, Refused
from boring_service_kit.pond import attach_heartbeat
from boring_service_kit.tools import READ_ONLY, RUNS, as_mcp_tool, default_state_dir, refuse_unknown_arguments

INSTRUCTIONS = (
    "Drives the D365 Finance and Operations web client in a signed-in Edge, like a person. web_open a menu "
    "item, then web_set fields, web_press buttons/tabs, web_lookup to pick from a lookup, web_filter a list, web_select_row in "
    "grids. Every step returns the current state: scope (form/dialog/lookup), fields (name -> value), "
    "readonly, missing (empty required), buttons, action_tabs, tabs, grids, messages and new_messages; "
    "web_set marks values D365 did not keep with differs. Names are data-dyn-controlname "
    "values; labels also work; 'Name[2]' or nth picks the third match top to bottom (grid rows). Hidden "
    "buttons sit in collapsed action tabs: press the tab first. A step that outlasts the call answers "
    "status=running: call web_wait; until web_wait has returned its result, every other call is refused "
    "as busy, even after the step finished. Steps change real data in the connected D365."
)
REFUSAL_CODES = {"refused", "busy", "not_found", "no_lookup", "no_session", "covered"}
BUDGET_MARGIN = 3.0


def _outcome(error: D365WebError) -> Outcome:
    cls = Refused if error.code in REFUSAL_CODES else Failed
    return cls(error.code, error.message, **error.details)


def create_server(client: WebClient, state_dir: Path | None = None, log_calls: bool = False,
                  token_verifier: Any = None, auth: Any = None, on_discover: Any = None,
                  auth_server_provider: Any = None, call_budget: float | None = None) -> MCPServer:
    """Readers: web_state, web_grid, web_wait, web_screenshot. Writers: everything that acts."""
    enforce_access = token_verifier is not None or auth_server_provider is not None
    if call_budget:
        client.budget = max(5.0, call_budget - BUDGET_MARGIN)
    state = state_dir or default_state_dir("boring-pondnt")
    client.state_dir = state
    mcp = MCPServer("boring-pondnt-d365web", instructions=INSTRUCTIONS, version=__version__,
                    token_verifier=token_verifier, auth=auth, auth_server_provider=auth_server_provider)
    attach_heartbeat(mcp, on_discover)
    log = CallLog(state / "calls-d365web.jsonl" if log_calls else None)

    def tool(annotations: Any, role: str) -> Any:
        def register(fn: Any) -> Any:
            def safe(*args: Any, **kwargs: Any) -> dict[str, Any]:
                try:
                    return fn(*args, **kwargs)
                except StillRunning as running:
                    return running.as_dict("the step is still running in the browser; call web_wait for its result")
                except D365WebError as error:
                    raise _outcome(error) from None
            safe.__name__, safe.__doc__, safe.__wrapped__ = fn.__name__, fn.__doc__, fn  # type: ignore[attr-defined]
            guarded = permitted(safe, role) if enforce_access else safe
            mcp.tool(annotations=annotations)(as_mcp_tool(guarded, log))
            return fn
        return register

    @tool(RUNS, "writer")
    def web_open(menu_item: str, company: str | None = None, kind: str | None = None) -> dict[str, Any]:
        """Open a menu item (e.g. PurchTableListPage, SalesTableListPage, WHSWorkTableListPage) in a company
        (default: the config's company, else the user's D365 default). kind: display (default), action or output
        menu item."""
        return client.open(menu_item, company, kind)

    @tool(READ_ONLY, "reader")
    def web_state(full: bool = False, max_rows: int = 20) -> dict[str, Any]:
        """The current state of the top-most form/dialog/lookup. full=true adds labels and other controls."""
        return client.state(full, max(1, min(max_rows, 200)))

    @tool(RUNS, "writer")
    def web_set(values: NameValuePairs) -> dict[str, Any]:
        """Type values into fields, in order, like a person (click, select all, type, Tab):
        [{"field": name, "value": text}, ...] in the order to type them. Checkboxes
        take yes/no. Grid cells: the column name, 'Name[1]' for the second visible row. Stops at the
        first new error or warning message (stopped_at; D365 rejects values with warnings). applied
        lists what each field shows afterwards."""
        return client.set(values)

    @tool(RUNS, "writer")
    def web_press(name: str, nth: int = 0) -> dict[str, Any]:
        """Click a button, action tab, FastTab or link by control name or label (OK, Cancel, Save,
        buttonConfirm, an action tab like Purchase). Disabled buttons are refused."""
        return client.press(name, nth)

    @tool(RUNS, "writer")
    def web_lookup(field: str, pick: str | None = None, nth: int = 0) -> dict[str, Any]:
        """Open a field's lookup. Without pick: the state shows the lookup grid. With pick: choose the
        row that has this value in any column (typed into the field to filter when not visible)."""
        return client.lookup(field, pick, nth)

    @tool(RUNS, "writer")
    def web_select_row(grid: str = "", row: int = 0, match: AnyMap = None) -> dict[str, Any]:
        """Select a grid row (rendered rows, top = 0) or the first row whose cells match {column: value}
        (column name or label), scrolling the grid to find it (at most 1000 rows). grid empty: the
        largest grid in scope. Clicks a non-link cell."""
        return client.select_row(grid, row, match)

    @tool(RUNS, "writer")
    def web_filter(value: str, column: str | None = None) -> dict[str, Any]:
        """Filter a list page with its quick filter: value in column (header label like 'Order
        number', or the column name). Without column: the first offered column. value '' clears."""
        return client.filter(value, column)

    @tool(READ_ONLY, "reader")
    def web_grid(grid: str = "", max_rows: int = 50, start_row: int = 0, all_rows: bool = False) -> dict[str, Any]:
        """Columns and rows of a grid in the current scope (empty name: the largest). D365 renders only
        rows near the screen: without start_row/all_rows you get those. start_row scrolls to later rows
        (more=true if any follow); all_rows scrolls through all, at most 1000 (truncated=true beyond)."""
        return client.grid(grid, max(1, min(max_rows, 200)), max(0, start_row), all_rows)

    @tool(RUNS, "writer")
    def web_key(keys: str) -> dict[str, Any]:
        """Press keys, space-separated chords: 'Escape', 'Alt+N', 'Ctrl+S', 'Alt+F9', 'Tab Tab Enter'.
        Goes to the focused element."""
        return client.key(keys)

    @tool(RUNS, "writer")
    def web_click(x: float, y: float) -> dict[str, Any]:
        """Fallback: click page coordinates (CSS pixels of a 1600x1000 window)."""
        return client.click(x, y)

    @tool(RUNS, "writer")
    def web_type(text: str) -> dict[str, Any]:
        """Fallback: type text into the focused element."""
        return client.type(text)

    @tool(RUNS, "writer")
    def web_run_scenario(steps: list[dict[str, Any]], variables: AnyMap = None,
                         stop_on_mismatch: bool = True, full_final: bool = False) -> dict[str, Any]:
        """Run steps in one go: {"op": open|state|set|press|lookup|filter|select_row|key, <that tool's
        arguments>, "expect": {...}, "save": {...}, "note": "..."}. expect: scope, form, caption
        (substring), fields {name or label: value}, editable [fields], message (substring of a new message), no_errors,
        enabled/disabled [buttons], grid {name, contains {column: value}, rows}. save {var: "caption" |
        "field:NAME" | "message" | "grid:COLUMN" or {"from", "regex", "row"}}; ${var} in later steps;
        variables seeds them. Long runs answer status=running: web_wait returns {ok, steps, variables,
        final} (final: scope, form, caption, messages; full_final=true for the whole page).
        ok=false at the first mismatch or error."""
        return client.run_scenario(steps, variables, stop_on_mismatch, full_final)

    @tool(READ_ONLY, "reader")
    def web_wait(seconds: float | None = None) -> dict[str, Any]:
        """The result (or error) of a step that answered status=running, once; until then other calls
        are refused as busy. Nothing pending: the state once idle."""
        return client.wait(seconds)

    @tool(READ_ONLY, "reader")
    def web_screenshot() -> dict[str, Any]:
        """Save a PNG of the page next to the server's state; returns its path on the server machine."""
        return client.screenshot()

    @tool(RUNS, "writer")
    def web_close() -> dict[str, Any]:
        """Close the browser this server started (frees memory); the next step starts it again."""
        return client.close_browser()

    refuse_unknown_arguments(mcp)
    mcp._d365web_client = client  # type: ignore[attr-defined]
    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="boring-pondnt-d365web", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="answer over HTTP, configured by --config (no stdio)")
    serve.add_argument("--d365web", type=Path, required=True, help="d365web.json: URL, company, Edge profile")
    serve.add_argument("--state-dir", type=Path)
    serve.add_argument("--no-call-log", action="store_true")
    add_config_argument(serve)
    args = parser.parse_args(argv)
    try:
        client = WebClient(Config.load(args.d365web))
        if not args.config:
            parser.error("serve requires --config: the d365web server speaks HTTP only")
        config = ServiceConfig(args.config)
        def make_server(auth: Any = None, on_discover: Any = None) -> MCPServer:
            return create_server(client, args.state_dir, not args.no_call_log, on_discover=on_discover,
                                 call_budget=config.call_limit, **(auth or {}))
        config.serve(make_server)
    except Outcome as outcome:
        print(json.dumps(outcome.as_dict()), file=sys.stderr)
        raise SystemExit(2) from None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "failed", "code": "config", "message": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
