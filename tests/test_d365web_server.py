"""d365web MCP tools over a stand-in client (the browser side is tested in test_d365web_client)."""

from __future__ import annotations

import asyncio
import json

from boring_pondnt.d365web_client import D365WebError, StillRunning
from boring_pondnt.d365web_server import create_server

TOOLS = {"web_open", "web_state", "web_set", "web_press", "web_lookup", "web_select_row", "web_grid", "web_key", "web_filter", "web_run_scenario",
         "web_click", "web_type", "web_wait", "web_screenshot", "web_close"}  # no WHS tools: d365whsapp form mode (D38)


class FakeClient:
    def __init__(self):
        self.budget, self.state_dir, self.calls = 25.0, None, []

    def _record(self, name, *args):
        self.calls.append((name, args))
        return {"scope": "form", "form": "PurchTable", "op": name}

    def open(self, menu_item, company=None, kind=None):
        if menu_item == "Slow":
            raise StillRunning("web_open", 25.0)
        return self._record("open", menu_item, company, kind)

    def state(self, full=False, max_rows=20):
        return self._record("state", full, max_rows)

    def set(self, values):
        raise D365WebError("not_found", "no visible field 'X' in the current form", similar=["XY"])

    def press(self, name, nth=0):
        raise D365WebError("signin_required", "profile 'alligator' is not signed in")

    def lookup(self, field, pick=None, nth=0):
        return self._record("lookup", field, pick, nth)

    def select_row(self, grid="", row=0, match=None):
        return self._record("select_row", grid, row, match)

    def run_scenario(self, steps, variables=None, stop_on_mismatch=True, full_final=False):
        return self._record("run_scenario", steps, variables, stop_on_mismatch, full_final)

    def whs_login(self, worker, password, company=None):
        return self._record("whs_login", worker, password, company)

    def whs_run_scenario(self, worker, steps, variables=None, stop_on_mismatch=True):
        return self._record("whs_run_scenario", worker, steps, variables, stop_on_mismatch)

    def whs_screen(self, worker):
        return self._record("whs_screen", worker)

    def whs_submit(self, worker, values=None, press=None):
        return self._record("whs_submit", worker, values, press)

    def whs_logoff(self, worker):
        return self._record("whs_logoff", worker)

    def filter(self, value, column=None):
        return self._record("filter", value, column)

    def grid(self, grid="", max_rows=50, start_row=0, all_rows=False):
        return self._record("grid", grid, max_rows, start_row, all_rows)

    def key(self, keys):
        return self._record("key", keys)

    def click(self, x, y):
        return self._record("click", x, y)

    def type(self, text):
        return self._record("type", text)

    def wait(self, seconds=None):
        return self._record("wait", seconds)

    def screenshot(self):
        return {"path": "x.png", "bytes": 1}

    def close_browser(self):
        return {"closed": True}


def call(server, name, args=None):
    result = asyncio.run(server.call_tool(name, args or {}))
    blocks = result[0] if isinstance(result, tuple) else result
    content = blocks.content if hasattr(blocks, "content") else blocks
    return getattr(blocks, "is_error", False), json.loads(content[0].text)


def test_tools_and_budget(tmp_path):
    client = FakeClient()
    server = create_server(client, tmp_path, log_calls=True, call_budget=28.0)
    assert {t.name for t in asyncio.run(server.list_tools())} == TOOLS
    assert client.budget == 25.0 and client.state_dir == tmp_path


def test_results_running_and_outcomes(tmp_path):
    client = FakeClient()
    server = create_server(client, tmp_path, log_calls=True)
    error, state = call(server, "web_open", {"menu_item": "PurchTableListPage"})
    assert not error and state["op"] == "open" and client.calls[-1] == ("open", ("PurchTableListPage", None, None))
    error, running = call(server, "web_open", {"menu_item": "Slow"})
    assert not error and running["status"] == "running" and "web_wait" in running["hint"]
    error, outcome = call(server, "web_set", {"values": {"X": "1"}})
    assert error and outcome["status"] == "refused" and outcome["code"] == "not_found"
    assert outcome["details"]["similar"] == ["XY"]
    error, outcome = call(server, "web_press", {"name": "OK"})
    assert error and outcome["status"] == "failed" and outcome["code"] == "signin_required"
    error, outcome = call(server, "web_state", {"max_rows": 5000})
    assert not error and client.calls[-1] == ("state", (False, 200))
    import pytest
    with pytest.raises(Exception, match="unknown: bogus"):
        call(server, "web_press", {"name": "OK", "bogus": 1})
    assert (tmp_path / "calls-d365web.jsonl").is_file()


def test_tool_schemas_are_strict_for_copilot(tmp_path):
    from strict_schema import assert_copilot_accepts
    assert_copilot_accepts(asyncio.run(create_server(FakeClient(), tmp_path).list_tools()))


def test_web_set_takes_every_form_and_advertises_field_value_objects(tmp_path):
    """Copilot gets [{field, value}, ...]; an object and [[name, value], ...] still pass through."""
    from boring_pondnt.d365web_client import normalize_values
    seen = []

    class Recording(FakeClient):
        def set(self, values):
            seen.append(normalize_values(values))
            return {"op": "set"}
    server = create_server(Recording(), tmp_path)
    for values in ([{"field": "A", "value": "1"}, {"field": "B", "value": "2"}], [["A", "1"], ["B", "2"]], {"A": "1", "B": "2"}):
        error, _ = call(server, "web_set", {"values": values})
        assert not error, values
    assert seen == [[("A", "1"), ("B", "2")]] * 3
    tool = next(t for t in asyncio.run(server.list_tools()) if t.name == "web_set")
    items = tool.input_schema["properties"]["values"]["items"]
    assert items["type"] == "object" and items["additionalProperties"] is False and items["required"] == ["field", "value"]


def test_web_grid_passes_paging(tmp_path):
    client = FakeClient()
    server = create_server(client, tmp_path)
    error, _ = call(server, "web_grid", {"start_row": 20, "max_rows": 20})
    assert not error and client.calls[-1] == ("grid", ("", 20, 20, False))
    error, _ = call(server, "web_grid", {"all_rows": True, "max_rows": 5000})
    assert not error and client.calls[-1] == ("grid", ("", 200, 0, True))
