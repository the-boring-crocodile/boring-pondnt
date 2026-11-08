"""MODEL_LIB's own rules, run in node against a small fake DOM and a fake `$dyn`.

The Python tests stub the page out; these pin what the JavaScript decides: which row a lookup pick
takes, which quick filter is used, when a date goes through SetValue, what a checkbox reads as.
The fake is not D365: it models only the calls MODEL_LIB makes (querySelectorAll by attributes,
closest, elementFromPoint, offsetParent, $dyn.context/peek/isObservable/callFunction).
Skipped where node is missing (the Windows suite); live behaviour is checked on a dev box."""
import json
import shutil
import subprocess

import pytest

from boring_pondnt.d365web_model import MODEL_LIB

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

FAKE = r"""
const ATTR = /\[([\w-]+)(?:="((?:[^"\\]|\\.)*)")?\]/g;
const parse = sel => Array.from(sel.matchAll(ATTR)).map(m => [m[1], m[2] === undefined ? null : m[2].replace(/\\(.)/g, '$1')]);
class El {
  constructor(attrs, ctx, kids, hidden) {
    this.attrs = attrs || {}; this.ctx = ctx; this.children = kids || []; this.parent = null;
    this.offsetParent = hidden ? null : {}; this.children.forEach(k => { k.parent = this; });
  }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  matches(sel) { return parse(sel).every(([k, v]) => k in this.attrs && (v === null || String(this.attrs[k]) === v)); }
  closest(sel) { for (let e = this; e; e = e.parent) if (e.matches && e.matches(sel)) return e; return null; }
  querySelectorAll(sel) {
    const out = [], walk = e => e.children.forEach(k => { if (k.matches(sel)) out.push(k); walk(k); });
    walk(this); return out;
  }
}
const obs = v => { const f = () => v; f.__obs = true; return f; };
globalThis.CSS = {escape: s => String(s).replace(/"/g, '\\"')};
globalThis.$dyn = {
  context: e => { if (!e || !e.ctx) throw new Error('no context'); return e.ctx; },
  isObservable: v => typeof v === 'function' && !!v.__obs,
  peek: v => v(),
  callFunction: (f, c, args) => f.apply(c, args),
};
const sent = [];
const cmd = name => function (...args) { sent.push([name, ...args]); };
const control = (name, ctx, extra) => new El(Object.assign({'data-dyn-controlname': name}, extra || {}), ctx);
const install = (root, at) => {
  globalThis.document = Object.assign(root, {elementFromPoint: () => at || null});
};
"""


def run(body: str):
    script = FAKE + "\nconst LIB = " + MODEL_LIB + ";\n" + body + "\n"
    done = subprocess.run([NODE, "-"], input=script, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def lookup(rows: str, pick: str):
    return run("""
      const list = control('Grid', {_typeName: 'ReactList', DataSource: {__array: %s},
                                    changeRow: cmd('changeRow'), HasNextPage: obs(false)});
      const popup = new El({'data-dyn-role': 'Popup', 'data-dyn-form-name': 'Lookup1'},
                           {SelectionCompleted: cmd('SelectionCompleted')}, [list]);
      install(new El({}, null, [popup]));
      console.log(JSON.stringify({r: LIB('lookupPick', [%s, 1000]), sent}));
    """ % (rows, json.dumps(pick)))


def test_a_lookup_pick_ignores_numbers_and_flags_in_other_columns():
    out = lookup("[{_properties: {Id: '2', Count: 1, Flag: true}}, {_properties: {Id: '1', Name: 'One'}}]", "1")
    assert out["r"]["acted"] == "changeRow" and out["r"]["index"] == 1
    assert out["r"]["values"] == ["1", "One"] and out["sent"] == [["changeRow", 1]]


def test_two_lookup_rows_holding_the_pick_are_not_guessed_between():
    out = lookup("[{_properties: {Id: 'A', Name: 'Bolt'}}, {_properties: {Id: 'B', Name: ' bolt '}}]", "Bolt")
    assert out["r"]["ambiguous"] == 2 and "acted" not in out["r"] and out["sent"] == []


def test_internal_lookup_properties_never_match():
    out = lookup("[{_properties: {RecId_internal: 'X1', Id: 'Y'}}]", "X1")
    assert "acted" not in out["r"] and out["r"]["complete"] is True


FILTER = """
  const qf = name => ({_typeName: 'QuickFilter', FieldList: obs(['F1']), FieldLabels: obs(['Location']),
    Field: cmd(name + ':Field'), InputValue: cmd(name + ':InputValue'),
    callApplyFilter: cmd(name + ':apply'), callClearFilter: cmd(name + ':clear')});
  const list = new El({'data-dyn-form-name': 'ListPage'}, null, [control('QuickFilter', qf('list'))]);
  const dialog = new El({'data-dyn-form-name': 'Dlg'}, null, [control('QuickFilter', qf('dialog'))]);
  install(new El({}, null, [list, dialog]));
"""


def test_the_quick_filter_is_the_scope_forms_not_the_first_in_the_page():
    out = run(FILTER + "console.log(JSON.stringify({r: LIB('filter', ['01', 'Location', 'Dlg']), sent}));")
    assert out["r"]["acted"] == "ApplyFilter"
    assert [s[0] for s in out["sent"]] == ["dialog:InputValue", "dialog:Field", "dialog:apply"]


def test_two_quick_filters_without_a_scope_form_are_not_guessed_between():
    out = run(FILTER + "console.log(JSON.stringify({r: LIB('filter', ['01', 'Location', '']), sent}));")
    assert "acted" not in out["r"] and out["sent"] == []


def at(ctx: str, op: str, args: str) -> dict:
    return run("""
      const el = control('C', %s);
      install(new El({}, null, [el]), el);
      console.log(JSON.stringify({r: LIB(%s, [1, 2, 'C', %s]), sent}));
    """ % (ctx, json.dumps(op), args))


@pytest.mark.parametrize("want,acted", [("2026-11-20", True), ("11/20/2026", False), ("2026-11-2", False)])
def test_a_date_goes_through_setvalue_only_in_iso(want, acted):
    out = at("{_typeName: 'Date', SetValue: cmd('SetValue')}", "set", json.dumps(want))
    assert ("acted" in out["r"]) is acted
    assert out["sent"] == ([["SetValue", want]] if acted else [])


@pytest.mark.parametrize("value", ["1", "true", "True"])
def test_a_checked_checkbox_reads_as_checked_whatever_the_value_form(value):
    out = at("{_typeName: 'CheckBox', Value: obs(%s), toggleValue: cmd('toggle')}" % json.dumps(value), "set", "'Yes', true")
    assert out["r"].get("unchanged") is True and out["sent"] == []


def test_a_combo_takes_the_option_by_label_and_refuses_an_unknown_one():
    combo = "{_typeName: 'ComboBox', Items: obs([{Label: 'Pick', Value: 1}, {Label: 'Put', Value: 2}]), SetSelectedItem: cmd('SetSelectedItem')}"
    out = at(combo, "set", "'put'")
    assert out["sent"] == [["SetSelectedItem", 2]]
    out = at(combo, "set", "'Move'")
    assert out["r"] == {"error": "no_option", "type": "ComboBox", "options": ["Pick", "Put"]} and out["sent"] == []


def test_a_tab_is_expanded_and_a_button_clicked():
    out = at("{_typeName: 'TabPage', Clicked: cmd('Clicked'), Expanded: obs(0), ExpandedChanged: cmd('ExpandedChanged')}", "press", "null")
    assert out["sent"] == [["ExpandedChanged", True]]
    out = at("{_typeName: 'CommandButton', Clicked: cmd('Clicked')}", "press", "null")
    assert out["sent"] == [["Clicked"]]


def test_nothing_under_the_point_means_no_action():
    out = run("""
      install(new El({}, null, [control('C', {_typeName: 'Input', SetValue: cmd('SetValue')})]), null);
      console.log(JSON.stringify({r: LIB('set', [1, 2, 'C', 'x']), sent}));
    """)
    assert "fallback" in out["r"] and out["sent"] == []


def test_raw_value_is_read_at_the_point_not_the_first_control_of_that_name():
    out = run("""
      const row1 = control('D', {Value: obs('2026-01-01')}), row2 = control('D', {Value: obs('2026-11-20')});
      install(new El({}, null, [row1, row2]), row2);
      console.log(JSON.stringify({r: LIB('rawValue', [1, 2, 'D']), sent}));
    """)
    assert out["r"] == {"value": "2026-11-20"}


def test_an_unknown_op_sends_nothing():
    out = at("{_typeName: 'Input', SetValue: cmd('SetValue')}", "magic", "null")
    assert out["r"] == {"error": "unknown op"} and out["sent"] == []


def test_a_picked_row_reports_its_numbers_too_for_the_readback():
    out = lookup("[{_properties: {Id: 'S1', Site: 2}}]", "S1")
    assert out["r"]["values"] == ["S1", "2"]


def test_grid_paging_uses_the_scope_forms_grid_only():
    out = run("""
      const grid = who => control('Grid', {LoadNextPage: cmd(who + ':load'), HasNextPage: obs(true), DataSource: {__array: []}});
      const behind = new El({'data-dyn-form-name': 'ListPage'}, null, [grid('behind')]);
      const dialog = new El({'data-dyn-form-name': 'Dlg'}, null, [grid('dialog')]);
      install(new El({}, null, [behind, dialog]));
      console.log(JSON.stringify({r: LIB('loadPage', ['Grid', 'Dlg']), sent}));
    """)
    assert out["sent"] == [["dialog:load"]]


def test_the_page_library_ships_as_its_own_file_and_parses():
    from boring_pondnt.d365web_client import LIB
    assert LIB.startswith("(function (op, a, secretNamePattern)") and LIB.endswith("})")
    done = subprocess.run([NODE, "-e", "const f = " + LIB + "; console.log(typeof f)"],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0 and done.stdout.strip() == "function", done.stderr



def test_a_reference_group_field_is_left_to_the_input_path():
    out = run("""
      const field = control('Worker_DirPerson_FK_Name', {_typeName: 'Input', SetValue: cmd('SetValue')});
      const group = new El({'data-dyn-controlname': 'Worker', 'data-dyn-role': 'ReferenceGroup'}, null, [field]);
      install(new El({}, null, [group]), field);
      console.log(JSON.stringify({r: LIB('set', [1, 2, 'Worker_DirPerson_FK_Name', 'Lars Giusti']), sent}));
    """)
    assert "reference group" in out["r"]["fallback"] and out["sent"] == []


def test_a_menu_button_is_left_to_a_click():
    out = at("{_typeName: 'MenuButton', Clicked: cmd('Clicked')}", "press", "null")
    assert "menu button" in out["r"]["fallback"] and out["sent"] == []
