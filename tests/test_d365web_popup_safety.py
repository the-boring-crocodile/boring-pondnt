"""Dropdown opening and failure cleanup against synthetic pages; no D365 environment."""
import json
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from boring_pondnt import d365web_client as web
from boring_pondnt.d365web_client import D365WebError, WebClient


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(web.time, "sleep", lambda _: None)


def combo(mode, options=("One", "Two"), disappears=False):
    state = SimpleNamespace(open=False, clicks=[], keys=[], checks=0, selected=None)
    client = object.__new__(WebClient)
    def click(x, y):
        state.clicks.append((x, y))
        if (x, y) == (1, 2):
            state.open = mode == "click"
        else:
            assert state.open
            state.selected = (x, y)
            state.open = False
    def key(value):
        state.keys.append(value)
        if value == "Alt+ArrowDown":
            state.open = not state.open if mode != "never" else False
        elif value == "Escape":
            assert state.open, "Escape would reach the underlying form"
            state.open = False
    page = SimpleNamespace(click=click, key=key)
    def js(op, *args):
        if op == "listOpen":
            assert args == ("combo",)
            state.checks += 1
            if mode == "delayed" and state.checks == 3:
                state.open = True
            return state.open
        assert op == "comboOption"
        if not state.open:
            return {"error": "not_found", "options": []}
        if args[0] in options:
            return {"x": 3, "y": 4}
        if disappears:
            state.open = False
        return {"error": "not_found", "options": list(options)}
    client._js = js
    return client, page, state


@pytest.mark.parametrize("mode,keys", [("click", []), ("delayed", []),
                                       ("keyboard", ["Alt+ArrowDown"])])
def test_combo_uses_click_opened_list_or_opens_it_once_with_alt_down(mode, keys):
    client, page, state = combo(mode)
    client._pick_combo(page, {"x": 1, "y": 2}, "Choice", "Two", [])
    assert state.keys == keys and state.selected == (3, 4)


def test_combo_with_no_list_sends_no_escape():
    client, page, state = combo("never")
    with pytest.raises(D365WebError) as caught:
        client._pick_combo(page, {"x": 1, "y": 2}, "Choice", "Two", [])
    assert caught.value.code == "refused" and caught.value.details["options"] == []
    assert state.keys == ["Alt+ArrowDown"]


@pytest.mark.parametrize("disappears,keys", [(False, ["Escape"]), (True, [])])
def test_combo_failure_checks_the_list_again_before_escape(disappears, keys):
    client, page, state = combo("click", disappears=disappears)
    with pytest.raises(D365WebError) as caught:
        client._pick_combo(page, {"x": 1, "y": 2}, "Choice", "Missing", [{"field": "Earlier"}])
    assert state.keys == keys
    assert caught.value.details["applied"] == [{"field": "Earlier"}]


@pytest.mark.parametrize("click_opens,keys", [(True, []), (False, ["Alt+ArrowDown"])])
def test_lookup_opening_does_not_toggle_a_list_opened_by_the_click(click_opens, keys):
    client = object.__new__(WebClient)
    state = SimpleNamespace(open=False, keys=[])
    def key(value):
        state.keys.append(value)
        assert value == "Alt+ArrowDown"
        state.open = not state.open
    page = SimpleNamespace(click=lambda *args: setattr(state, "open", click_opens), key=key)
    client._locate = lambda *args: {"x": 1, "y": 2}
    client._wait_idle = lambda: True
    client._js = lambda op, *args: state.open if op == "listOpen" else {
        "scope": "lookup" if state.open else "form"}
    client._open_lookup(page, "Account", 0, None)
    assert state.open and state.keys == keys


def test_lookup_filter_typing_closes_only_the_list_opened_by_the_click():
    client = object.__new__(WebClient)
    state = SimpleNamespace(open=False, keys=[], typed=[])
    def key(value):
        state.keys.append(value)
        if value == "Escape":
            assert state.open
            state.open = False
        elif value == "Ctrl+a":
            assert not state.open
        else:
            assert value == "Alt+ArrowDown"
            state.open = True
    page = SimpleNamespace(click=lambda *args: setattr(state, "open", True), key=key,
                           type_text=state.typed.append)
    client._locate = lambda *args: {"x": 1, "y": 2}
    client._wait_idle = lambda: True
    client._js = lambda op, *args: state.open if op == "listOpen" else {
        "scope": "lookup" if state.open else "form"}
    client._open_lookup(page, "Account", 0, "1003")
    assert state.keys == ["Escape", "Ctrl+a", "Alt+ArrowDown"] and state.typed == ["1003"]


@pytest.mark.parametrize("disappears,keys", [(False, ["Escape", "Escape"]), (True, [])])
def test_lookup_failure_checks_the_popup_before_each_escape(disappears, keys):
    client = object.__new__(WebClient)
    client.config = SimpleNamespace(backend="input")  # the input path; the model path sends no keys
    state = SimpleNamespace(open=True, keys=[], reopens=[])
    def key(value):
        assert value == "Escape" and state.open, "Escape would reach the underlying form"
        state.keys.append(value)
        state.open = False
    client._ensure = lambda: SimpleNamespace(key=key)
    client._wait_idle = lambda: True
    def reopen(page, name, nth, typed, explicit=False):
        state.reopens.append(typed)
        state.open = True
    client._open_lookup = reopen
    def js(op, *args):
        if op == "messages": return []
        if op == "scope": return {"scope": "lookup"}
        if op == "listOpen":
            assert args == ("lookup",)
            return state.open
        if op == "grid": return {"rows": []}
        if op == "locate": return {"value": "Missing"}  # the typed filter text is left in the field
        assert op == "row"
        if disappears:
            state.open = False
        return {"error": "not_found"}
    client._js = js
    with pytest.raises(D365WebError) as caught:
        client._op_lookup("Account", 0, "Missing")
    assert caught.value.code == "not_found" and caught.value.details["now"] == "Missing"
    assert state.keys == keys and state.reopens == ["Missing"]


@pytest.mark.parametrize("open_list,keys", [(True, ["Ctrl+a", "Escape"]), (False, ["Ctrl+a"])])
def test_quick_filter_failure_never_escapes_a_form(open_list, keys):
    client = object.__new__(WebClient)
    client.config = SimpleNamespace(backend="input")  # the input path; the model path sends no keys
    sent = []
    def key(value):
        if value == "Escape":
            assert open_list
        sent.append(value)
    page = SimpleNamespace(click=lambda *args: None, key=key, type_text=lambda _: None)
    client._ensure = lambda: page
    def js(op, *args):
        if op == "messages": return []
        if op == "quickfilter": return {"x": 1, "y": 2}
        if op == "listOpen":
            assert args == ("filter",)
            return open_list
        assert op == "filteroption"
        return {"error": "not_found", "columns": ["Item"]}
    client._js = js
    with pytest.raises(D365WebError) as caught:
        client._op_filter("A1", None)
    assert caught.value.code == "not_found" and sent == keys


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable for synthetic DOM")
def test_long_combo_list_scrolls_the_matching_option_before_finding_its_click_point():
    # Execute the emitted DOM code: offscreen options exist but cannot be clicked until scrolled.
    script = """
        const lib = %s;
        let scrolled = -1, open = true;
        globalThis.getComputedStyle = () => ({visibility:'visible', display:'block'});
        const options = Array.from({length: 200}, (_, index) => ({
            innerText: 'Choice ' + index,
            getBoundingClientRect: () => ({left:20, top:index === scrolled ? 50 : 5000,
                width:open ? 100 : 0, height:open ? 20 : 0}),
            scrollIntoView: () => {scrolled = index;},
            contains: () => false
        }));
        const list = {getBoundingClientRect: () => ({width:open ? 150 : 0, height:100})};
        globalThis.document = {
            querySelectorAll: selector => selector === '.comboBox-list' ? [list] : options,
            elementFromPoint: () => scrolled >= 0 ? options[scrolled] : null
        };
        const before = lib('listOpen', ['combo']);
        const point = lib('comboOption', ['Choice 199']);
        open = false;
        console.log(JSON.stringify({before, point, scrolled, after:lib('listOpen', ['combo'])}));
    """ % web.LIB
    result = subprocess.run([shutil.which("node")], input=script, capture_output=True, text=True,
                            timeout=5, check=True)
    answer = json.loads(result.stdout)
    assert answer == {"before": True, "point": {"x": 70, "y": 60}, "scrolled": 199, "after": False}
