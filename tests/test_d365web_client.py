"""d365web client: units without a browser, then the D365 layer against a fake page in a real Chromium/Edge.

The browser tests need a Chromium-family browser: D365WEB_TEST_BROWSER, else the Playwright Chromium or a
Chrome/Edge on PATH; without one they are skipped.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from boring_pondnt.d365web_browser import Browser, launch_args
from boring_pondnt.d365web_client import (MENU_ITEM, Config, D365WebError, StillRunning, WebClient, as_bool,
                                          normalize_values, parse_field_key, value_differs)

FAKE = Path(__file__).parent / "d365web_fake"


# --- units -------------------------------------------------------------------------------------------

def test_launch_args_keep_a_visible_window_rendering(tmp_path):
    """A visible Edge nobody sees must keep rendering (2026-10-30: clicks after open were lost on a test box)."""
    visible = launch_args(tmp_path, 9333, headless=False)
    headless = launch_args(tmp_path, 9333, headless=True)
    for args in (visible, headless):
        assert len([a for a in args if a.startswith("--disable-features=")]) == 1  # Chromium uses the last one
        assert "--remote-debugging-port=9333" in args and f"--user-data-dir={tmp_path}" in args
    assert "--disable-features=msEdgeSidebarV2,msUndersideButton,CalculateNativeWinOcclusion" in visible
    assert {"--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding"} <= set(visible)
    assert "--headless=new" not in visible
    assert "--headless=new" in headless and "--disable-features=msEdgeSidebarV2,msUndersideButton" in headless
    assert not any("backgrounding" in a for a in headless)


def test_field_keys_and_booleans():
    assert parse_field_key("PurchLine_ItemId") == ("PurchLine_ItemId", 0)
    assert parse_field_key("PurchLine_ItemId[2]") == ("PurchLine_ItemId", 2)
    assert parse_field_key("Vendor account") == ("Vendor account", 0)
    assert as_bool("Yes") and as_bool("ja") and not as_bool("no") and not as_bool("")
    with pytest.raises(D365WebError):
        as_bool("maybe")


def test_values_keep_their_order():
    assert normalize_values({"b": 1, "a": 2}) == [("b", 1), ("a", 2)]
    assert normalize_values([["Site", "1"], ["Warehouse", "11"]]) == [("Site", "1"), ("Warehouse", "11")]
    assert normalize_values([{"field": "Site", "value": "1"}]) == [("Site", "1")]
    for bad in ([], {}, [["a"]], [[1, 2]], "a=b"):
        with pytest.raises(D365WebError):
            normalize_values(bad)


def test_value_differs_allows_d365_formatting():
    for typed, now in (("125", "125.00"), ("2,388", "2388.00"), ("618160", "618160-001---"), ("us-901", "US-901"),
                       ("", ""), ("Net10 ", "Net10")):
        assert not value_differs(typed, now), (typed, now)
    for typed, now in (("BPT-001", ""), ("125", "12.50"), ("ABC", "XYZ"), ("", "x")):
        assert value_differs(typed, now), (typed, now)


def test_menu_items():
    for good in ("PurchTableListPage", "action:PurchFormLetter_Confirm", "output:x_1"):
        assert MENU_ITEM.match(good)
    for bad in ("../x", "Purch&mi=x", "javascript:alert(1)", ""):
        assert not MENU_ITEM.match(bad)


def test_config_load(tmp_path):
    path = tmp_path / "d365web.json"
    path.write_text(json.dumps({"url": "https://aos.example.com/namespaces/x", "profile": "alligator", "debug_port": 9333}))
    config = Config.load(path)
    assert config.url == "https://aos.example.com" and config.company == "" and config.headless
    assert config.profile_dir().name == "alligator"
    for bad in ({"url": "http://aos"}, {"url": "https://aos", "profile": "../x"}, {"url": "https://aos", "debug_port": 80},
                {"url": "https://aos", "password": "x"}, {"url": "https://aos", "company": "us mf"}):
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            Config.load(path)


def test_budget_running_wait_and_busy():
    client = WebClient(Config(url="https://aos"), budget=0.3)
    release = threading.Event()
    with pytest.raises(StillRunning) as running:
        client.call("web_press", lambda: (release.wait(5), {"done": 1})[1])
    assert running.value.as_dict("")["status"] == "running" and running.value.operation == "web_press"
    with pytest.raises(D365WebError) as busy:
        client.call("web_set", lambda: {})
    assert busy.value.code == "busy" and "web_press" in busy.value.message
    release.set()
    assert client.wait(5) == {"done": 1}
    assert client.call("web_state", lambda: {"next": 2}) == {"next": 2}
    client.stop.set()


def test_errors_surface_from_the_worker():
    client = WebClient(Config(url="https://aos"))
    def fail():
        raise D365WebError("not_found", "no visible field 'X'", similar=["Y"])
    with pytest.raises(D365WebError) as error:
        client.call("web_set", fail)
    assert error.value.details == {"similar": ["Y"]}
    client.stop.set()


def _finished_uncollected(client, label, fn):
    """Start fn so it answers running, then let it finish without collecting it."""
    release, finished = threading.Event(), threading.Event()

    def step():
        release.wait(5)
        try:
            return fn()
        finally:
            finished.set()
    with pytest.raises(StillRunning):
        client.call(label, step)
    release.set()
    assert finished.wait(5)
    time.sleep(0.05)  # the future settles after fn returns


@pytest.mark.parametrize("follow", ["web_state", "web_set"])
def test_a_finished_result_is_kept_until_web_wait(follow):
    """Review finding F3: a step that answered running and finished in the background is not replaced by
    the next call (a state read or an acting one); web_wait returns that step's own result."""
    client = WebClient(Config(url="https://aos"), budget=0.2)
    _finished_uncollected(client, "web_press", lambda: {"receipt": "PO-1 posted"})
    ran = []
    with pytest.raises(D365WebError) as busy:
        client.call(follow, lambda: ran.append(1) or {"state": 1})
    assert busy.value.code == "busy" and "web_press" in busy.value.message and "web_wait" in busy.value.message
    assert ran == []  # refused before it ran
    assert client.wait(1) == {"receipt": "PO-1 posted"}
    assert client.call(follow, lambda: {"state": 2}) == {"state": 2}  # collected: the next call runs
    client.stop.set()


def test_a_finished_error_is_kept_until_web_wait_and_collected_once():
    client = WebClient(Config(url="https://aos"), budget=0.2)

    def fail():
        raise D365WebError("browser", "posting outcome unknown", browser_code="closed")
    _finished_uncollected(client, "web_press", fail)
    with pytest.raises(D365WebError) as busy:
        client.call("web_state", lambda: {"state": 1})
    assert busy.value.code == "busy"
    with pytest.raises(D365WebError) as error:
        client.wait(1)
    assert error.value.code == "browser" and "posting outcome unknown" in error.value.message
    assert client.call("web_state", lambda: {"state": 2}) == {"state": 2}  # the error counted as collected
    client.stop.set()


# --- against a fake D365 page --------------------------------------------------------------------------

def find_browser() -> str | None:
    if os.environ.get("D365WEB_TEST_BROWSER"):
        return os.environ["D365WEB_TEST_BROWSER"]
    for pattern in ("/opt/pw-browsers/chromium-*/chrome-linux/chrome", "/opt/pw-browsers/chromium"):
        hits = sorted(Path("/").glob(pattern.lstrip("/")))
        if hits and hits[-1].is_file():
            return str(hits[-1])
    # Windows: Edge is not on PATH (the release's run-tests.cmd skipped all 33 browser tests on the test box)
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        edge = Path(base or "", "Microsoft", "Edge", "Application", "msedge.exe")
        if base and edge.is_file():
            return str(edge)
    for name in ("chromium", "chromium-browser", "google-chrome", "msedge"):
        if shutil.which(name):
            return shutil.which(name)
    return None


BROWSER = find_browser()
needs_browser = pytest.mark.skipif(BROWSER is None, reason="no Chromium-family browser")


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # every path is the fake client, like D365's single page
        menu = parse_qs(urlsplit(self.path).query).get("mi", [""])[0]
        self.path = {"PaneFixture": "/action-pane.html", "TabFixture": "/tab-target.html"}.get(menu, "/fake.html")
        super().do_GET()


@pytest.fixture(scope="module")
def site():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(FAKE)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def client(site, tmp_path):
    def launch(config):
        args = ("--no-sandbox",) if hasattr(os, "geteuid") and os.geteuid() == 0 else ()
        return Browser.launch(tmp_path / "profile", headless=True, edge=BROWSER, extra_args=args)
    web = WebClient(Config(url=site, step_timeout=10), browser_factory=launch, budget=20, state_dir=tmp_path)
    yield web
    web.shutdown()


@needs_browser
def test_open_and_state(client):
    state = client.open("PurchTableListPage")
    assert state["scope"] == "form" and "company" not in state  # no company anywhere: the user's default
    state = client.open("PurchTableListPage", "USMF")
    assert state["scope"] == "form" and state["form"] == "PurchTable" and state["company"] == "usmf"
    assert state["caption"] == "00000001 : 1001 - Acme" and state["title"] == "All purchase orders"
    assert state["fields"]["OrderAccount"] == "" and state["readonly"] == {"VendorName": ""}
    assert state["fields"]["OneTime"] == "No" and state["fields"]["GenerateLP"] == "Yes"
    assert "buttonConfirm" in state["buttons"] and state["disabled"] == ["buttonNever"]
    assert state["fields"]["QuickFilter"] == ""
    assert state["action_tabs"] == ["PurchOrder (Purchase order)"]
    (grid,) = state["grids"]
    assert grid["name"] == "LineSpec" and [c["name"] for c in grid["columns"]][:2] == ["PurchLine_LineNumber", "PurchLine_ItemId"]
    # visual order, not DOM order; the pooled row outside the viewport is not a row
    assert [r["values"] for r in grid["rows"]] == [["1", "A0001", "5"], ["2", "A0002", "7"]]
    full = client.state(full=True)
    assert any(f["name"] == "OrderAccount" and f["label"] == "Vendor account" for f in full["fields"])


@needs_browser
def test_set_reads_back_and_reports_new_messages(client):
    client.open("PurchTableListPage")
    state = client.set({"OrderAccount": "1002", "OneTime": "yes"})
    assert state["applied"] == [{"field": "OrderAccount", "value": "1002", "now": "1002"},
                                {"field": "OneTime", "value": "yes", "now": "Yes"}]
    assert state["readonly"]["VendorName"] == "Lande Packaging Supplies" and "new_messages" not in state
    state = client.set({"Vendor account": "ZZZZ", "OneTime": "no"})  # label works too
    assert state["stopped_at"] == "Vendor account" and state["fields"]["OneTime"] == "Yes"  # stopped before
    assert [a["field"] for a in state["applied"]] == ["Vendor account"]
    assert state["new_messages"][0]["severity"] == "warning" and "'ZZZZ'" in state["new_messages"][0]["text"]
    with pytest.raises(D365WebError) as error:
        client.set({"VendorName": "x"})
    assert error.value.code == "refused" and "read-only" in error.value.message
    with pytest.raises(D365WebError) as error:
        client.set({"Vendor": "x"})
    assert error.value.code == "not_found" and "OrderAccount (Vendor account)" in error.value.details["similar"]


@needs_browser
def test_grid_cells_by_nth_and_row_selection(client):
    client.open("PurchTableListPage")
    state = client.set({"PurchLine_Qty[1]": "11"})
    assert state["grids"][0]["rows"][1]["values"] == ["2", "A0002", "11"]
    state = client.select_row("LineSpec", match={"Item": "A0002"})
    assert state["selected"] == {"grid": "LineSpec", "row": 1}
    assert state["grids"][0]["rows"][1].get("selected") is True
    with pytest.raises(D365WebError) as error:
        client.select_row("LineSpec", match={"PurchLine_ItemId": "GHOST"})
    assert error.value.code == "not_found"
    grid = client.grid("LineSpec")
    assert grid["visible_rows"] == 2


@needs_browser
def test_dialog_scope_and_lookup(client):
    client.open("PurchTableListPage")
    state = client.press("New")
    assert state["scope"] == "dialog" and state["form"] == "PurchCreateOrder"
    assert state["fields"] == {"OrderAccount": ""} and state["buttons"] == ["OK", "Cancel"]
    state = client.set({"OrderAccount": "1001"})  # the dialog's field, not the form's
    assert state["fields"] == {"OrderAccount": "1001"}
    state = client.press("Cancel")
    assert state["scope"] == "form" and state["fields"]["OrderAccount"] == ""
    state = client.lookup("OrderAccount")
    assert state["scope"] == "lookup" and state["form"] == "VendTableLookup"
    assert [r["values"][0] for r in state["grids"][0]["rows"]] == ["1001", "1002"]
    state = client.lookup("OrderAccount", pick="1003")  # not in view: typed to filter
    assert state["scope"] == "form" and state["fields"]["OrderAccount"] == "1003"
    assert state["readonly"]["VendorName"] == "Ade Supply Company"


@needs_browser
def test_progress_dialog_counts_as_busy(client):
    client.open("PurchTableListPage")
    started = time.monotonic()
    state = client.press("buttonConfirm")
    assert time.monotonic() - started >= 1.9  # progress dialog, then aria-busy on the main area
    assert state["scope"] == "form" and state["new_messages"] == [{"severity": "info", "text": "Operation completed"}]
    assert state["messages"] == []  # not repeated
    with pytest.raises(D365WebError) as error:
        client.press("buttonNever")
    assert error.value.code == "refused"


@needs_browser
def test_keys_screenshot_and_close(client):
    client.open("PurchTableListPage")
    client.lookup("OrderAccount")
    state = client.key("Escape")
    assert state["scope"] == "form"
    shot = client.screenshot()
    assert Path(shot["path"]).read_bytes()[:4] == b"\x89PNG"
    assert client.close_browser() == {"closed": True}
    assert client.state()["form"] == ""  # a new browser on about:blank


@needs_browser
def test_quick_filter(client):
    client.open("PurchTableListPage")
    state = client.filter("A0002", "Item")
    assert state["filter"] == {"column": "Item", "value": "A0002"}
    assert [r["values"][1] for r in state["grids"][0]["rows"]] == ["A0002"]
    state = client.filter("7", "PurchLine_Qty")  # a column name works too
    assert state["filter"]["column"] == "Quantity" and state["grids"][0]["rows"][0]["values"][1] == "A0002"
    with pytest.raises(D365WebError) as error:
        client.filter("x", "Colour")
    assert error.value.code == "not_found" and error.value.details["columns"] == ["Line", "Item", "Quantity"]
    state = client.filter("")
    assert len(state["grids"][0]["rows"]) == 2


@needs_browser
def test_message_box(client):
    client.open("PurchTableListPage")
    state = client.press("LineStripDelete")
    assert state["scope"] == "dialog" and state["buttons"] == ["Yes", "No"] and state["form"] == "SysBoxForm"
    assert state["caption"].endswith("Delete record?")
    state = client.press("No")
    assert state["scope"] == "form" and len(state["grids"][0]["rows"]) == 2
    client.press("LineStripDelete")
    state = client.press("Yes")
    assert [r["values"][1] for r in state["grids"][0]["rows"]] == ["A0001"]


@needs_browser
def test_combo_box_picks_from_its_list(client):
    client.open("PurchTableListPage")
    state = client.set([["Unitization", "None"]])
    assert state["applied"] == [{"field": "Unitization", "value": "None", "now": "None"}]
    with pytest.raises(D365WebError) as error:
        client.set({"Unitization": "Pallets"})
    assert error.value.code == "refused" and error.value.details["options"] == ["None", "License plate grouping", "User defined"]
    assert client.state()["fields"]["Unitization"] == "None"


@needs_browser
def test_combo_box_scrolls_to_an_option_below_the_fold(client):
    """A long enum list scrolls in its popup; an option outside the visible part was clicked at its
    clipped position, nothing was picked and the value stayed None (Activity code, test box, 2026-10-26)."""
    client.open("PurchTableListPage")
    state = client.set([["WorkActivity", "Demo receiving"]])
    assert state["applied"] == [{"field": "WorkActivity", "value": "Demo receiving", "now": "Demo receiving"}]


@needs_browser
def test_toggle(client):
    client.open("PurchTableListPage")
    state = client.set([["GenerateLP", "no"]])
    assert state["applied"] == [{"field": "GenerateLP", "value": "no", "now": "No"}]
    state = client.set([["GenerateLP", "no"]])  # already off: nothing clicked
    assert state["fields"]["GenerateLP"] == "No"


@needs_browser
def test_a_teaching_tip_over_a_control_is_acknowledged_other_covers_refuse(client):
    """A click must hit its control: D365's "What's new" tip over the Show resolved checkbox swallowed it
    (test box, 2026-10-26). The tip is acknowledged with its own button and reported; anything else
    lying over the control refuses, and nothing is clicked."""
    client.open("PurchTableListPage")
    client.press("TipButton")
    state = client.set([["GenerateLP", "no"]])
    assert state["applied"] == [{"field": "GenerateLP", "value": "no", "now": "No"}]
    assert state["dismissed"] and "Save queries to these views" in state["dismissed"][0]
    client.press("BlockButton")
    with pytest.raises(D365WebError) as error:
        client.set([["OneTime", "yes"]])
    assert error.value.code == "covered"
    assert client.state()["fields"]["OneTime"] == "No"


@needs_browser
def test_a_scenario_reports_the_teaching_tip_it_acknowledged(client):
    result = client.run_scenario([
        {"op": "open", "menu_item": "PurchTableListPage"},
        {"op": "press", "name": "TipButton"},
        {"op": "set", "values": [["GenerateLP", "no"]], "expect": {"fields": {"GenerateLP": "No"}}},
    ])
    assert result["ok"], result
    assert "Save queries to these views" in result["steps"][2]["dismissed"][0]


@needs_browser
def test_required_fields_and_silently_dropped_values(client):
    state = client.open("PurchTableListPage")
    assert state["missing"] == ["AccountNum"] and "185" not in state["buttons"]
    state = client.set([["AccountNum", "BPT-001"]])
    assert state["applied"] == [{"field": "AccountNum", "value": "BPT-001", "now": "", "differs": True}]
    state = client.set([["AccountNum", "US-901"]])
    assert "differs" not in state["applied"][0] and "missing" not in state


@needs_browser
def test_menu_button_flyout_and_labels(client):
    state = client.open("PurchTableListPage")
    assert state["labels"]["LineStripDelete"] == "Remove" and state["labels"]["OneTime"] == "One-time supplier"
    assert "VendorName" not in state["labels"] and "SaveAndOpenButton" not in state["labels"]  # names say it
    state = client.press("SaveAndOpenButton")
    assert state["menu"] == ["saveAndOpenQuotation (Sales quotation)", "saveAndOpenSalesOrder (Sales order)"]
    state = client.press("Sales order")  # the open menu wins over the form's buttons
    assert state["pressed"] == "saveAndOpenSalesOrder" and "menu" not in state
    assert state["new_messages"] == [{"severity": "info", "text": "Opened Sales order"}]


@pytest.fixture
def pane_client(client):
    assert client.open("PaneFixture", "USMF")["form"] == "PaneForm"
    return client


@needs_browser
@pytest.mark.parametrize("name", ["ViewHistory", "Employment history"])
def test_pinned_and_detached_action_pane_share_discovery_and_targeting(pane_client, name):
    client = pane_client
    assert client.press(name)["readonly"]["Result"] == "history"  # pinned
    client.press("TogglePin")  # unpinned, collapsed
    client.call("fixture", lambda: client._ensure().evaluate("document.getElementById('result').value = ''"))
    assert "ViewHistory" not in client.state()["buttons"]
    with pytest.raises(D365WebError) as error:
        client.press(name)
    assert error.value.code == "not_found"  # no fallback into the other form's visible flyout
    client.press("WorkTab")  # temporary expansion
    full = client.state(full=True)
    assert [b["name"] for b in full["buttons"]].count("ViewHistory") == 1
    assert "ForeignOnly" not in [b["name"] for b in full["buttons"]]
    assert client.state()["labels"]["ViewHistory"] == "Employment history"
    assert client.press(name)["readonly"]["Result"] == "history"
    with pytest.raises(D365WebError) as error:
        client.press("ViewHistory", nth=1)
    assert error.value.code == "not_found"
    with pytest.raises(D365WebError) as error:
        client.press("DisabledAction")
    assert error.value.code == "refused"
    client.press("TogglePin")
    client.call("fixture", lambda: client._ensure().evaluate("document.getElementById('result').value = ''"))
    assert client.state()["buttons"].count("ViewHistory") == 1  # moved back, no duplicate root
    assert client.press(name)["readonly"]["Result"] == "history"


@needs_browser
@pytest.mark.parametrize("kind", ["dialog", "lookup"])
def test_detached_action_pane_does_not_escape_modal_scope(pane_client, kind):
    client = pane_client
    client.press("TogglePin")
    client.press("WorkTab")
    assert client.press("Open" + kind.title())["scope"] == kind
    full = client.state(full=True)
    assert [b["name"] for b in full["buttons"]] == ["ViewHistory"]
    state = client.press("Employment history")
    assert state["scope"] == "form" and state["readonly"]["Result"] == kind


@needs_browser
@pytest.mark.parametrize("change", [
    "document.getElementById('pane_WorkTab_flyout').style.display='none'",
    "document.getElementById('pane_WorkTab_flyout').setAttribute('aria-hidden','true')",
    "document.getElementById('work').setAttribute('aria-expanded','false')",
    "document.getElementById('pane_WorkTab_flyout').id='unassociated_flyout'",
])
def test_hidden_collapsed_or_unassociated_panes_are_not_candidates(pane_client, change):
    client = pane_client
    client.press("TogglePin")
    client.press("WorkTab")
    client.call("fixture", lambda: client._ensure().evaluate(change))
    assert "ViewHistory" not in client.state()["buttons"]
    with pytest.raises(D365WebError) as error:
        client.press("ViewHistory")
    assert error.value.code == "not_found"



@pytest.fixture
def tab_client(client):
    assert client.open("TabFixture", "USMF")["form"] == "TabFixture"
    return client


@needs_browser
@pytest.mark.parametrize("role", ["TabPage", "SectionPage", "AppBarTab", "Tab"])
@pytest.mark.parametrize("name", ["UsersTabPage", "User mappings"])
def test_named_tab_targets_its_header_instead_of_a_nested_link(tab_client, role, name):
    client = tab_client
    client.call("fixture", lambda: client._ensure().evaluate(
        "document.getElementById('tab').setAttribute('data-dyn-role'," + json.dumps(role) + ")"))
    state = client.press(name)
    assert state["form"] == "TabFixture"
    assert state["readonly"]["Result"] == "header"
    assert client.state(full=True)["tabs" if role != "AppBarTab" else "action_tabs"][0]["label"] == "User mappings"


@needs_browser
@pytest.mark.parametrize("change", [
    "document.getElementById('header').style.display='none'",
    "document.getElementById('header').remove()",
    "document.getElementById('tab').appendChild(document.getElementById('header').cloneNode(true))",
    "const h=document.getElementById('header'); const nested=document.createElement('div'); "
    "nested.setAttribute('data-dyn-role','SectionPage'); h.parentElement.appendChild(nested); nested.appendChild(h)",
])
def test_tab_with_no_unique_visible_own_header_refuses_without_clicking(tab_client, change):
    client = tab_client
    client.call("fixture", lambda: client._ensure().evaluate("(()=>{" + change + "})()"))
    with pytest.raises(D365WebError) as error:
        client.press("UsersTabPage")
    assert error.value.code == "refused"
    assert error.value.details["reason"] == "tab_header_unavailable"
    assert client.state()["form"] == "TabFixture" and client.state()["readonly"]["Result"] == ""


@needs_browser
def test_disabled_tab_header_refuses_and_ordinary_nested_link_still_works(tab_client):
    client = tab_client
    client.call("fixture", lambda: client._ensure().evaluate("document.getElementById('header').disabled=true"))
    with pytest.raises(D365WebError) as error:
        client.press("UsersTabPage")
    assert error.value.code == "refused" and client.state()["readonly"]["Result"] == ""
    state = client.press("LinkedUser")
    assert state["form"] == "LinkedUserForm" and state["readonly"]["Result"] == "linked-user"


@needs_browser
def test_a_tab_header_covered_by_its_own_content_is_not_a_valid_hit(tab_client):
    client = tab_client
    client.call("fixture", lambda: client._ensure().evaluate("""(()=>{
        const h=document.getElementById('header').getBoundingClientRect();
        const link=document.getElementById('linked');
        Object.assign(link.style,{position:'fixed',left:h.left+'px',top:h.top+'px',
            width:h.width+'px',height:h.height+'px',zIndex:9});
    })()"""))
    with pytest.raises(D365WebError) as error:
        client.press("UsersTabPage")
    assert error.value.code == "covered"
    assert client.state()["form"] == "TabFixture" and client.state()["readonly"]["Result"] == ""


@needs_browser
def test_a_named_tab_control_can_itself_be_the_header(tab_client):
    client = tab_client
    client.call("fixture", lambda: client._ensure().evaluate("""(()=>{
        const root=document.getElementById('tab'), h=document.getElementById('header');
        root.removeAttribute('data-dyn-role');root.removeAttribute('data-dyn-controlname');
        h.setAttribute('data-dyn-role','TabPage');h.setAttribute('data-dyn-controlname','UsersTabPage');
    })()"""))
    state = client.press("UsersTabPage")
    assert state["form"] == "TabFixture" and state["readonly"]["Result"] == "header"


@pytest.fixture
def pivot_client(tab_client):
    """D365 PivotItem: a possibly hidden panel and a sibling ARIA-associated header."""
    tab_client.call("fixture", lambda: tab_client._ensure().evaluate("""(()=>{
        const panel=document.getElementById('tab'), head=document.getElementById('header');
        panel.setAttribute('data-dyn-role','PivotItem');panel.setAttribute('role','tabpanel');
        panel.setAttribute('aria-labelledby','header');head.setAttribute('aria-controls','tab');
        head.setAttribute('data-dyn-controlname','UsersTabPage_header');
        head.removeAttribute('aria-expanded');head.setAttribute('aria-selected','true');
        panel.parentElement.insertBefore(head,panel);
        head.onclick=()=>{panel.style.display='block';head.setAttribute('aria-selected','true');
            document.getElementById('result').value='header';};
    })()"""))
    return tab_client


@needs_browser
@pytest.mark.parametrize("active", [True, False])
@pytest.mark.parametrize("name", ["UsersTabPage", "User mappings"])
def test_pivot_tab_targets_associated_header_even_when_panel_is_hidden(pivot_client, active, name):
    client = pivot_client
    if not active:
        client.call("fixture", lambda: client._ensure().evaluate(
            "document.getElementById('tab').style.display='none';"
            "document.getElementById('header').setAttribute('aria-selected','false')"))
    tabs = client.state(full=True)["tabs"]
    assert len(tabs) == 1 and tabs[0]["name"] == "UsersTabPage"
    assert tabs[0]["label"] == "User mappings" and tabs[0]["selected"] is active
    state = client.press(name)
    assert state["form"] == "TabFixture" and state["readonly"]["Result"] == "header"
    assert client.state(full=True)["tabs"][0]["selected"] is True


@needs_browser
@pytest.mark.parametrize("change", [
    "document.getElementById('tab').removeAttribute('aria-labelledby')",
    "document.getElementById('header').setAttribute('aria-controls','other')",
    "document.getElementById('header').setAttribute('role','button')",
    "const second=document.getElementById('header').cloneNode(true);second.id='second-header';"
    "document.getElementById('header').after(second);"
    "document.getElementById('tab').setAttribute('aria-labelledby','header second-header')",
    "document.getElementById('header').style.display='none'",
    "const form=document.createElement('form');form.setAttribute('data-dyn-role','Form');"
    "document.body.appendChild(form);form.appendChild(document.getElementById('header'))",
])
def test_pivot_without_visible_same_scope_associated_header_refuses(pivot_client, change):
    client = pivot_client
    client.call("fixture", lambda: client._ensure().evaluate("(()=>{" + change + "})()"))
    with pytest.raises(D365WebError) as error:
        client.press("UsersTabPage")
    assert error.value.code == "refused"
    assert error.value.details["reason"] == "tab_header_unavailable"
    assert client.state()["form"] == "TabFixture" and client.state()["readonly"]["Result"] == ""


@needs_browser
def test_disabled_pivot_header_refuses_and_explicit_record_link_still_works(pivot_client):
    client = pivot_client
    client.call("fixture", lambda: client._ensure().evaluate("document.getElementById('header').disabled=true"))
    with pytest.raises(D365WebError) as error:
        client.press("UsersTabPage")
    assert error.value.code == "refused" and client.state()["readonly"]["Result"] == ""
    assert client.press("LinkedUser")["form"] == "LinkedUserForm"


# --- scenarios -------------------------------------------------------------------------------------

def test_substitute_check_steps_and_variables():
    from boring_pondnt.d365web_client import check_expectation, check_steps, extract_variable, substitute
    assert substitute({"values": [["PO", "${po}"]], "n": 1}, {"po": "DOC-1"}) == {"values": [["PO", "DOC-1"]], "n": 1}
    with pytest.raises(D365WebError):
        substitute("${nope}", {})
    for bad in ([], [{"op": "jump"}], [{"op": "press", "name": "OK", "value": 1}],
                [{"op": "press", "name": "OK", "expect": {"colour": "red"}}]):
        with pytest.raises(D365WebError):
            check_steps(bad)
    check_steps([{"op": "open", "menu_item": "X", "expect": {"form": "Y"}, "save": {"a": "caption"}, "note": "n"}])
    state = {"scope": "form", "form": "PurchTable", "caption": "00000273 : 1001 - Acme",
             "fields": {"Qty": "10.00"}, "readonly": {"PurchStatus": "Open order", "Proforma": "Yes"},
             "labels": {"Proforma": "Posting", "buttonUpdateConfirm": "Confirm now"},
             "buttons": ["buttonUpdateConfirm"], "disabled": ["buttonConfirm"],
             "new_messages": [{"severity": "info", "text": "Created PO DOC-00000287 for M1"}],
             "grids": [{"name": "Lines", "columns": [{"name": "ItemId", "label": "Item"}, {"name": "Qty", "label": "Quantity"}],
                        "rows": [{"row": 0, "values": ["A0001", "5.00"]}]}]}
    good = {"scope": "form", "caption": "00000273", "fields": {"Qty": "10", "Posting": "yes", "PurchStatus": "Open order"},
            "message": "doc-00000287", "no_errors": True, "enabled": ["Confirm now"], "disabled": ["buttonConfirm"],
            "grid": {"name": "Lines", "rows": 1, "contains": {"Item": "A0001", "Quantity": "5"}}}
    assert check_expectation(state, good) == []
    bad = check_expectation(state, {"form": "SalesTable", "fields": {"Qty": "11", "Nope": "x"}, "message": "error",
                                    "enabled": ["buttonConfirm"], "grid": {"contains": {"Item": "B"}},
                                    "editable": ["PurchStatus"]})
    assert len(bad) == 7 and "editable: PurchStatus is read-only" in bad
    assert check_expectation(state, {"editable": ["Qty"]}) == []
    assert extract_variable(state, {"from": "message", "regex": r"PO (\S+)"}) == "DOC-00000287"
    assert extract_variable(state, {"from": "caption", "regex": r"^\S+"}) == "00000273"
    assert extract_variable(state, "field:Posting") == "Yes"
    assert extract_variable(state, {"from": "grid:Quantity", "grid": "Lines"}) == "5.00"
    assert extract_variable(state, "field:Missing") is None


@needs_browser
def test_scenario_runs_expects_and_saves(client):
    steps = [
        {"op": "open", "menu_item": "PurchTableListPage", "expect": {"form": "PurchTable", "caption": "00000001"},
         "save": {"order": {"from": "caption", "regex": r"^\S+"}}},
        {"op": "lookup", "field": "OrderAccount", "pick": "${vendor}",
         "expect": {"fields": {"Name": "Lande Packaging Supplies"}, "no_errors": True}},
        {"op": "press", "name": "buttonConfirm", "expect": {"message": "operation completed"}},
        {"op": "select_row", "grid": "LineSpec", "match": {"Item": "A0002"},
         "expect": {"grid": {"name": "LineSpec", "rows": 2, "contains": {"Item": "A0002", "Quantity": "7"}}},
         "save": {"qty": {"from": "grid:Quantity", "grid": "LineSpec", "row": 1}}},
        {"op": "set", "values": [["Vendor account", "ZZZZ"]], "expect": {"no_errors": True}},
        {"op": "state"},
    ]
    result = client.run_scenario(steps, {"vendor": "1002"})
    assert not result["ok"]
    assert [line["step"] for line in result["steps"]] == [1, 2, 3, 4, 5]
    assert result["variables"] == {"vendor": "1002", "order": "00000001", "qty": "7"}
    assert "mismatch" not in result["steps"][3] and result["steps"][2]["messages"] == ["info: Operation completed"]
    assert "no_errors" in result["steps"][4]["mismatch"][0] and result["final"]["form"] == "PurchTable"
    assert "fields" not in result["final"]  # a summary unless full_final
    result = client.run_scenario(steps[:1] + [{"op": "press", "name": "Nope"}])
    assert not result["ok"] and result["steps"][1]["error"] == "not_found"


@needs_browser
def test_hyperlink_cells_are_clicked_on_their_text(client):
    client.open("PurchTableListPage")
    state = client.press("PurchLine_LineNumber", 1)  # the second row's link: "2", a short text in a wide cell
    assert state["new_messages"] == [{"severity": "info", "text": "Opened 2"}]
    state = client.select_row("LineSpec", match={"Item": "A0001"})  # selecting a row avoids the link cell
    assert state["selected"]["row"] == 0 and "new_messages" not in state


@needs_browser
def test_a_row_of_only_hyperlinks_is_selected_beside_the_link_or_refused(client):
    """select_row on a list whose only column is a link clicked the link text and opened the record;
    the next Delete then hit that record (test box setup form, 2026-10-26). A free point of the row is
    clicked instead; a row the link covers completely refuses and clicks nothing."""
    client.open("DemoMenuLists")
    state = client.select_row("MenuList", match={"Menu item": "Demo receiving"})
    assert state["selected"] == {"grid": "MenuList", "row": 1}
    time.sleep(0.4)  # the fake opens a record 150 ms after the click
    assert not [m for m in client.state()["messages"] if m["text"].startswith("Opened")]
    assert client.state()["scope"] == "form"  # the pointer left the row: no hover preview in the way
    with pytest.raises(D365WebError) as error:
        client.select_row("WideLinkList", 0)
    assert error.value.code == "refused" and "only hyperlink cells" in error.value.message
    assert "new_messages" not in client.state()


@needs_browser
def test_a_grid_whose_next_block_comes_only_with_the_keyboard(client):
    """A custom receiving log on the test box (2026-10-26) keeps 30 rows in the DOM and fetches the next block
    only when the selection moves past the last one: the scroll scan stopped at 30 of 43 rows and three
    regression checks missed rows that were there. The scan pages on with the keyboard while the grid
    says it has more rows than were seen."""
    client.open("DemoKeyLog")
    grid = client.grid("KeyGrid", max_rows=50, all_rows=True)
    assert len(grid["rows"]) == 45 and grid["rows"][-1]["values"][0] == "ISO-000045"
    client.open("DemoKeyLog")
    state = client.select_row("KeyGrid", match={"Document": "ISO-000041"})
    assert state["selected"]["grid"] == "KeyGrid"
    result = client.run_scenario([{"op": "open", "menu_item": "DemoKeyLog"},
                                  {"op": "state", "expect": {"grid": {"name": "KeyGrid",
                                                                      "contains": {"Document": "ISO-000044"}}}}])
    assert result["ok"], result


@needs_browser
def test_launch_on_a_fixed_port(tmp_path):
    """Edge 155 writes no DevToolsActivePort for a fixed port; launch must find the port itself."""
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    args = ("--no-sandbox",) if hasattr(os, "geteuid") and os.geteuid() == 0 else ()
    browser = Browser.launch(tmp_path / "fixed", headless=True, edge=BROWSER, extra_args=args, port=port)
    try:
        assert browser.version()["product"]
        again = Browser.attach(port, tmp_path / "fixed")  # what a restarted server does
        assert again.version()["product"]
        again.conn.close()
    finally:
        browser.close()


@needs_browser
def test_whs_form_mode_through_d365whsapp(client, tmp_path):
    """d365whsapp in form mode on this WebClient (D38): the worker's own tab, the same tools and steps,
    `mode` in every result, what the form cannot see in not_checked; the main tab stays untouched."""
    from boring_pondnt.d365whsapp_backends import FormBackend
    from boring_pondnt.d365whsapp_server import create_server
    from test_d365whsapp_server import call

    server = create_server(FormBackend(client), tmp_path / "state", log_calls=True, call_budget=30)
    client.open("PurchTableListPage")
    error, page = call(server, "whs_login", {"worker": "whsapp05", "password": "1", "company": "usmf"})
    assert not error and page["mode"] == "form" and page["worker"] == "WHSAPP05" and page["resumed"] is False
    assert page["page"] == "Menu" and page["title"] == "Main Menu" and "Log off" in page["buttons"]
    assert client.state()["form"] == "PurchTable"  # the main tab is untouched
    error, page = call(server, "whs_submit", {"worker": "WHSAPP05", "press": "Inbound"})
    assert not error and page["title"] == "Inbound" and "DemoReceiving" in page["buttons"]
    error, run = call(server, "whs_run_scenario", {"worker": "WHSAPP05", "variables": {"doc": "doc-00000001"}, "steps": [
        {"press": "DemoReceiving", "expect": {"step": "DemoPageS1", "title": "Demo receiving"}},
        {"values": {"DemoScanId": "NOPE"}, "press": "OK",
         "expect": {"step": "DemoPageS1", "message": "No inbound document", "message_kind": "error"}},
        {"values": {"DemoScanId": "${doc}"}, "press": "OK", "expect": {"step": "DemoPageS5"}},
    ]})
    assert not error and run["mode"] == "form" and run["ok"] is True, run
    assert run["not_checked"] == ["message_kind (the form shows info and error messages alike)"]
    assert run["steps"][1]["message"] == {"text": "No inbound document found"}
    error, screen = call(server, "whs_screen", {"worker": "WHSAPP05"})
    assert screen["fields"] == {"DemoItemScan": ""} and screen["labels"]["DemoPageS5"] == "Scan item or barcode"
    error, out = call(server, "whs_logoff", {"worker": "WHSAPP05"})  # S5 → Leave → confirm (not Back) → S1 → ... → Log off
    assert not error and out == {"mode": "form", "worker": "WHSAPP05", "logged_off": True}
    error, outcome = call(server, "whs_screen", {"worker": "WHSAPP05"})
    assert error and outcome["code"] == "no_session"
    error, outcome = call(server, "whs_run_scenario", {"worker": "WHSAPP05", "steps": [{"press": "OK"}]})
    assert error and outcome["code"] == "no_session"
    error, outcome = call(server, "whs_login", {"worker": "WHSAPP05", "password": "wrong", "company": "usmf"})
    assert error and outcome["code"] == "login_failed"
    error, log = call(server, "whs_log", {"worker": "WHSAPP05", "lines": 500})
    assert "(form)" in log["log"] and "press DemoReceiving" in log["log"] and "wrong" not in log["log"]


@needs_browser
def test_whs_form_mode_uses_the_start_tab(client, tmp_path):
    """Wald, watching: Edge's empty start tab sat next to the worker's tab and came back to the front after
    every step. The first worker now takes the start tab; logoff empties it rather than closing Edge's
    last tab."""
    from boring_pondnt.d365whsapp_backends import FormBackend
    from boring_pondnt.d365whsapp_server import create_server
    from test_d365whsapp_server import call

    def tabs():
        targets = client.executor.submit(lambda: client.browser.conn.send("Target.getTargets")).result(10)
        return [t["url"] for t in targets["targetInfos"] if t["type"] == "page"]

    server = create_server(FormBackend(client), tmp_path / "state", log_calls=False, call_budget=30)
    error, page = call(server, "whs_login", {"worker": "whsapp05", "password": "1", "company": "usmf"})
    assert not error and page["page"] == "Menu"
    assert len(tabs()) == 1 and "WHSWorkExecute" in tabs()[0]  # one tab: the worker's
    error, page = call(server, "whs_login", {"worker": "whsapp06", "password": "1", "company": "usmf"})
    assert not error and len(tabs()) == 2  # a second worker gets a tab of its own
    for worker in ("WHSAPP06", "WHSAPP05"):
        error, out = call(server, "whs_logoff", {"worker": worker})
        assert not error and out["logged_off"] is True
    for _ in range(50):  # the navigation to about:blank lands asynchronously
        if tabs() == ["about:blank"]:
            break
        time.sleep(0.1)
    assert tabs() == ["about:blank"]  # the start tab stays, emptied
    error, page = call(server, "whs_login", {"worker": "whsapp05", "password": "1", "company": "usmf"})
    assert not error and len(tabs()) == 1  # and is taken again


# --- a long, virtualized list: rows beyond the rendered ones are reached by scrolling --------------------

@needs_browser
def test_grid_pages_and_all_rows_by_scrolling(client):
    client.open("DemoLongLog")
    shown = client.grid("LogGrid")
    assert 0 < len(shown["rows"]) < 10 and all("key" in r for r in shown["rows"])  # only the rendered rows
    page = client.grid("LogGrid", max_rows=5, start_row=20)
    assert [r["values"][0] for r in page["rows"]] == [f"DOC-{n:08d}" for n in range(21, 26)] and page["more"]
    every = client.grid("LogGrid", all_rows=True)
    assert len(every["rows"]) == 40 and every["more"] is False and "truncated" not in every  # the next block loaded
    assert every["rows"][20]["values"] == ["DOC-00000021", "Batch mismatch", "Open"]


@needs_browser
def test_select_row_and_expect_search_beyond_the_rendered_rows(client):
    client.open("DemoLongLog")
    state = client.select_row("LogGrid", match={"Document": "DOC-00000021"})
    assert "selected" in state
    result = client.run_scenario([
        {"op": "open", "menu_item": "DemoLongLog",
         "expect": {"grid": {"name": "LogGrid", "contains": {"Document": "DOC-00000033", "Status": "Open"}}}},
        {"op": "open", "menu_item": "DemoLongLog",
         "expect": {"grid": {"name": "LogGrid", "contains": {"Document": "DOC-00000099"}}}}])
    lines = result["steps"]
    assert "mismatch" not in lines[0]
    assert any("searched 40 rows" in m for m in lines[1]["mismatch"])
