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
from typing import Annotated, Any
from pydantic import Field

from mcp.server.mcpserver import MCPServer

from . import __version__
from .d365web_client import MENU_ITEM, MENU_KINDS, SCENARIO_STEPS_MAX, STEP_ARGS, Config, D365WebError, StillRunning, WebClient
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
    "grids, web_tree in trees. Every step returns the current state: scope (form/dialog/lookup), fields (name -> value), "
    "readonly, missing (empty required), buttons, action_tabs, tabs, grids, messages and new_messages; "
    "web_set marks values D365 did not keep with differs. Names are data-dyn-controlname "
    "values; labels also work; 'Name[2]' or nth picks the third match top to bottom (grid rows). Hidden "
    "buttons sit in collapsed action tabs: press the tab first. A step that outlasts the call answers "
    "status=running: call web_wait; until web_wait has returned its result, every other call is refused "
    "as busy, even after the step finished. Steps change real data in the connected D365."
)
REFUSAL_CODES = {"refused", "busy", "not_found", "no_lookup", "no_session", "covered"}
BUDGET_MARGIN = 3.0


# Each server its own name: support bundles collect several state folders flat.
CALL_LOG = "calls-d365web.jsonl"


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
    log = CallLog(state / CALL_LOG if log_calls else None)

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
    def web_open(
        menu_item: Annotated[str, Field(
            description=(
                "Technical menu item name, such as PurchTableListPage. A display:, action: or "
                "output: prefix also works without kind."
            ),
            json_schema_extra={"pattern": MENU_ITEM.pattern},
        )],
        company: Annotated[str | None, Field(
            description=(
                "Legal entity code, 1–8 letters or digits. Empty or omitted uses the configured "
                "company or the signed-in user's default."
            ),
        )] = None,
        kind: Annotated[str | None, Field(
            description="Menu item kind. Omit when supplying a prefix on menu_item.",
            json_schema_extra={"enum": (*MENU_KINDS, None)},
        )] = None,
    ) -> dict[str, Any]:
        """Open or run a D365 menu item in the browser. Use web_wait if it returns running."""
        return client.open(menu_item, company, kind)

    @tool(READ_ONLY, "reader")
    def web_state(
        full: Annotated[bool, Field(description="Include labels and other controls.")] = False,
        max_rows: Annotated[int, Field(
            description="Rows per grid. Values below 1 or above 200 are capped to that range.",
        )] = 20,
    ) -> dict[str, Any]:
        """Read the foremost form, dialog or lookup. Use web_wait if a previous call is still running."""
        return client.state(full, max(1, min(max_rows, 200)))

    @tool(RUNS, "writer")
    def web_set(
        values: Annotated[NameValuePairs, Field(
            description=(
                "Ordered {field, value} objects. Name[1] targets the second visible grid row. Use "
                "yes/no for checkboxes."
            ),
        )],
    ) -> dict[str, Any]:
        """Enter fields in order. Stops at the first new error or warning. Use web_wait if it returns running."""
        return client.set(values)

    @tool(RUNS, "writer")
    def web_press(
        name: Annotated[str, Field(description="Control name or visible label from web_state.")],
        nth: Annotated[int, Field(
            description="Zero-based occurrence when more than one control matches.",
            json_schema_extra={"minimum": 0},
        )] = 0,
    ) -> dict[str, Any]:
        """Click an enabled button, tab or link. Use web_wait if it returns running."""
        return client.press(name, nth)

    @tool(RUNS, "writer")
    def web_lookup(
        field: Annotated[str, Field(description="Field name or visible label from web_state.")],
        pick: Annotated[str | None, Field(
            description="Find this value in any column, filtering if needed. Omit to open the lookup grid.",
        )] = None,
        nth: Annotated[int | None, Field(
            description="Zero-based occurrence when more than one control matches. Omit for a grid "
                        "line's field: the active line.",
            json_schema_extra={"minimum": 0},
        )] = None,
    ) -> dict[str, Any]:
        """Open a field lookup or select a value. Use web_wait if it returns running."""
        return client.lookup(field, pick, nth)

    @tool(RUNS, "writer")
    def web_select_row(
        grid: Annotated[str, Field(description="Grid name from web_state. Empty selects the largest visible grid.")] = "",
        row: Annotated[int, Field(
            description="Zero-based rendered row. With match, select the first match instead.",
            json_schema_extra={"minimum": 0},
        )] = 0,
        match: Annotated[AnyMap, Field(
            description=(
                "Column names or labels mapped to values. Scrolls through up to 1,000 rows to find "
                "the first match."
            ),
        )] = None,
    ) -> dict[str, Any]:
        """Select a grid row by clicking a non-link cell. Use web_wait if it returns running."""
        return client.select_row(grid, row, match)

    @tool(RUNS, "writer")
    def web_tree(
        path: Annotated[list[str], Field(
            description="Node labels from the root down, as web_state's trees show them, e.g. [\"Bank\", \"Cheques\"].",
            json_schema_extra={"minItems": 1, "maxItems": 20},
        )],
        tree: Annotated[str, Field(description="Tree name from web_state. Empty: the only visible tree.")] = "",
    ) -> dict[str, Any]:
        """Select a tree node, expanding its parents. Never checks node checkboxes. Use web_wait if it returns running."""
        return client.tree(path, tree)

    @tool(RUNS, "writer")
    def web_filter(
        value: Annotated[str, Field(description="Text to filter by. Empty clears the filter.")],
        column: Annotated[str | None, Field(
            description="Column name or header label. Omit for the first available column.",
        )] = None,
    ) -> dict[str, Any]:
        """Apply a list page's quick filter. Use web_wait if it returns running."""
        return client.filter(value, column)

    @tool(READ_ONLY, "reader")
    def web_grid(
        grid: Annotated[str, Field(description="Grid name from web_state. Empty selects the largest visible grid.")] = "",
        max_rows: Annotated[int, Field(description="Rows to return, capped to 1–200 unless all_rows is true.")] = 50,
        start_row: Annotated[int, Field(
            description=(
                "Zero-based row to start at. Negative values act as zero. Zero alone reads the "
                "rendered rows."
            ),
        )] = 0,
        all_rows: Annotated[bool, Field(
            description="Scroll through the grid and read up to 1,000 rows. Overrides start_row and max_rows.",
        )] = False,
    ) -> dict[str, Any]:
        """Read grid columns and rows. If more, advance start_row by rows returned. Narrow with web_filter if truncated."""
        return client.grid(grid, max(1, min(max_rows, 200)), max(0, start_row), all_rows)

    @tool(RUNS, "writer")
    def web_key(
        keys: Annotated[str, Field(description="Space-separated chords such as Alt+N, Ctrl+S or Tab Tab Enter.")],
    ) -> dict[str, Any]:
        """Send key chords to the focused control. Use web_wait if it returns running."""
        return client.key(keys)

    @tool(RUNS, "writer")
    def web_click(
        x: Annotated[float, Field(description="Horizontal CSS pixels in the browser viewport.")],
        y: Annotated[float, Field(description="Vertical CSS pixels in the browser viewport.")],
    ) -> dict[str, Any]:
        """Click at page coordinates. Prefer web_press when you know the control name. Poll web_wait if running."""
        return client.click(x, y)

    @tool(RUNS, "writer")
    def web_type(text: str) -> dict[str, Any]:
        """Type text into the focused control. Use web_set to name a field or web_wait if this returns running."""
        return client.type(text)

    @tool(RUNS, "writer")
    def web_run_scenario(
        steps: Annotated[list[dict[str, Any]], Field(
            description=(
                "Each step takes op and that tool's arguments. Optional note, expect (scope, form, "
                "caption, fields, editable, message, no_errors, enabled, disabled or grid), and "
                "save (variable mapped to caption, field:NAME, message, grid:COLUMN or {from, "
                "regex, row, grid}). grid:COLUMN uses the first grid containing that column. "
                "Set grid to select a specific grid."
            ),
            json_schema_extra={
                "minItems": 1,
                "maxItems": SCENARIO_STEPS_MAX,
                "items": {"type": "object", "properties": {"op": {"type": "string", "enum": tuple(STEP_ARGS)}}, "required": ["op"]},
            },
        )],
        variables: Annotated[AnyMap, Field(
            description="Initial values for ${name} in steps. save can provide values for later steps.",
        )] = None,
        stop_on_mismatch: Annotated[bool, Field(
            description="Stop on the first failed expectation. A step error stops the scenario regardless.",
        )] = True,
        full_final: Annotated[bool, Field(description="Include the full final page instead of its summary.")] = False,
    ) -> dict[str, Any]:
        """Run browser steps in order. When running, collect with web_wait before starting another browser action."""
        return client.run_scenario(steps, variables, stop_on_mismatch, full_final)

    @tool(READ_ONLY, "reader")
    def web_wait(
        seconds: Annotated[float | None, Field(
            description="Seconds to wait for the browser. Omit to use its call budget.",
        )] = None,
    ) -> dict[str, Any]:
        """Collect the pending browser result once. Repeat while running. If idle, read the current page."""
        if seconds is not None:  # never past the call's own deadline: a result collected then would be lost
            seconds = max(0.0, seconds)
            if client.budget:
                seconds = min(seconds, client.budget)
        return client.wait(seconds)

    @tool(READ_ONLY, "reader")
    def web_screenshot() -> dict[str, Any]:
        """Save a PNG screenshot on the server machine. Read the returned path with file_read_bytes."""
        return client.screenshot()

    @tool(RUNS, "writer")
    def web_close() -> dict[str, Any]:
        """Close this service's browser. The next browser action can start it again."""
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
