"""The `model` interaction backend: act through the D365 web client's own control viewmodels.

The D365 web client keeps one viewmodel per control (`$dyn.context(element)`): its value, options,
validity and server commands (`SetValue`, `SetSelectedItem`, `toggleValue`, `Clicked`,
`ExpandedChanged`). Acting through them sends the same server request a person's input sends --
validation and dependent fields run on the server -- without clicks, focus, popups or key chords.
The control is the one at the located element's position (elementFromPoint), so a covered control
has no viewmodel here and that step uses input. In a grid, only the active row's controls are acted
on: D365 applies a command to the grid's current record. Explored live on 10.0.49.

Ownership: the WebClient still locates controls (label, name, nth, visibility, read-only grace),
waits for D365 to be idle (`$dyn.clientBusy`) and reads values and messages back. This module only
turns a located control into one model action. It returns None when the model cannot act on the
control, so the caller can use the `input` backend for that one step and say so.

The viewmodel is undocumented client internals: every action checks that the expected command
exists and refuses or falls back otherwise; it never guesses.
"""
from __future__ import annotations

import json
from typing import Any, Callable

BACKENDS = ("model", "input")

# One expression; called as (MODEL_LIB)(op, args). Returns plain JSON.
MODEL_LIB = r"""(function (op, a) {
  const ctxAt = (x, y, control) => {
    const hit = document.elementFromPoint(x, y);
    const el = hit && hit.closest('[data-dyn-controlname="' + CSS.escape(control) + '"]');
    if (!el || typeof $dyn === 'undefined' || !$dyn.context) return null;
    try { return $dyn.context(el); } catch (e) { return null; }
  };
  const peek = v => { try { return ($dyn.isObservable && $dyn.isObservable(v)) ? $dyn.peek(v) : v; } catch (e) { return undefined; } };
  const isCmd = f => typeof f === 'function' || (f && $dyn.isObservable && $dyn.isObservable(f) && typeof $dyn.peek(f) === 'function');
  const call = (c, name, args) => { $dyn.callFunction(c[name], c, args || []); };
  // a[1] of the grid ops: the current scope's form; only its grid, never a same-named one behind a dialog
  const gridIn = (name, form) => {
    let root = document;
    if (form) { root = Array.from(document.querySelectorAll('[data-dyn-form-name="' + CSS.escape(form) + '"]')).filter(e => e.offsetParent !== null).pop(); if (!root) return null; }
    return Array.from(root.querySelectorAll('[data-dyn-controlname="' + CSS.escape(name) + '"]')).find(e => e.offsetParent !== null) || null;
  };
  const TEXT = new Set(['Input', 'MultilineInput', 'Real', 'Integer', 'Int64', 'Date', 'DateTime', 'Guid', 'Segmented']);
  if (op === 'loadPage') {  // a read: asks the server for the grid's next block; moves no selection
    const el = gridIn(a[0], a[1]);
    let g = null; try { g = el && $dyn.context(el); } catch (e) { g = null; }
    if (!g || !isCmd(g.LoadNextPage)) return {fallback: 'grid without LoadNextPage'};
    const rows = () => ((g.DataSource && g.DataSource.__array) || []).length;
    if (!peek(g.HasNextPage)) return {acted: 'none', has_next: false, rows: rows()};
    call(g, 'LoadNextPage');
    return {acted: 'LoadNextPage', has_next: true, rows: rows()};
  }
  if (op === 'filter') {  // a[0] value ('' clears), a[1] column name or label ('' = the filter's default field),
    // a[2] the current scope's form: only its quick filter, never one of a form behind a dialog
    let root = document;
    if (a[2]) {
      root = Array.from(document.querySelectorAll('[data-dyn-form-name="' + CSS.escape(a[2]) + '"]')).filter(e => e.offsetParent !== null).pop();
      if (!root) return {fallback: 'scope form not found'};
    }
    const qfs = Array.from(root.querySelectorAll('[data-dyn-controlname]')).filter(e => e.offsetParent !== null)
      .map(e => { try { return $dyn.context(e); } catch (x) { return null; } })
      .filter(c => c && c._typeName === 'QuickFilter');
    if (qfs.length !== 1) return {fallback: qfs.length ? 'more than one quick filter in the scope' : 'no quick filter viewmodel'};
    const qf = qfs[0];
    if (!['callApplyFilter', 'callClearFilter', 'InputValue', 'Field'].every(k => typeof qf[k] === 'function')) return {fallback: 'quick filter without its commands'};
    const names = peek(qf.FieldList) || [], labels = peek(qf.FieldLabels) || [];
    if (!Array.isArray(names) || !Array.isArray(labels)) return {fallback: 'quick filter fields unreadable'};
    let i = a[1] ? names.indexOf(a[1]) : names.indexOf(peek(qf.Field));
    if (i < 0 && a[1]) i = labels.findIndex(l => String(l).toLowerCase() === String(a[1]).toLowerCase());
    if (i < 0) return a[1] ? {error: 'no_column', columns: labels} : {fallback: 'default field unknown'};
    if (!a[0]) { qf.callClearFilter(); return {acted: 'callClearFilter', cleared: true}; }
    qf.InputValue(String(a[0])); qf.Field(names[i]);
    qf.callApplyFilter();  // sends the control's ApplyFilter command to the server
    return {acted: 'ApplyFilter', column: labels[i] || names[i], field: names[i]};
  }
  const lookupForm = () => {  // the open lookup popup (the last one shown) and its list
    const f = Array.from(document.querySelectorAll('[data-dyn-role="Popup"][data-dyn-form-name]')).filter(e => e.offsetParent !== null).pop();
    if (!f) return null;
    let fc = null, g = null;
    try { fc = $dyn.context(f); } catch (e) { fc = null; }
    for (const e of f.querySelectorAll('[data-dyn-controlname]')) {
      let c = null; try { c = $dyn.context(e); } catch (x) { c = null; }
      if (c && c._typeName === 'ReactList' && c.DataSource) { g = c; break; }
    }
    return fc && g ? {fc, g, form: f.getAttribute('data-dyn-form-name')} : null;
  };
  if (op === 'openLookup') {
    const c = ctxAt(a[0], a[1], a[2]);
    if (!c || typeof c.openLookup !== 'function') return {fallback: 'no openLookup on the control'};
    c.openLookup();
    return {acted: 'openLookup'};
  }
  if (op === 'lookupPick') {  // a[0]: a value in any column of the row (exact, ignoring case and outer spaces)
    const L = lookupForm();
    if (!L || typeof L.g.changeRow !== 'function' || !isCmd(L.fc.SelectionCompleted)) return {fallback: 'no lookup viewmodel'};
    const want = String(a[0]).trim().toLowerCase();
    const arr = L.g.DataSource.__array || [];
    // text values only: an enum, flag or count in a hidden column must not match a pick such as "1"
    const texts = r => (r && r._properties ? Object.entries(r._properties) : [])
      .filter(([k]) => !/_internal$|^_RESERVED|TrackingId$/.test(k)).map(([, v]) => peek(v)).filter(v => typeof v === 'string');
    const hits = [];
    arr.forEach((r, j) => { if (texts(r).some(v => v.trim().toLowerCase() === want)) hits.push(j); });
    if (hits.length > 1) return {fallback: 'more than one lookup row holds that value', ambiguous: hits.length, rows: arr.length};
    const i = hits.length ? hits[0] : -1;
    if (i < 0) {
      if (peek(L.g.HasNextPage) && isCmd(L.g.LoadNextPage) && arr.length < (a[1] || 1000)) { call(L.g, 'LoadNextPage'); return {loading: true, rows: arr.length}; }
      return {fallback: 'no row with that value in the lookup data', rows: arr.length, complete: !peek(L.g.HasNextPage)};
    }
    L.g.changeRow(i);
    const shown = Object.entries(arr[i]._properties).filter(([k]) => !/_internal$|^_RESERVED|TrackingId$/.test(k))
      .map(([, v]) => peek(v)).filter(v => typeof v === 'string' || typeof v === 'number').map(v => String(v).trim()).filter(v => v);
    return {acted: 'changeRow', index: i, form: L.form, values: shown.slice(0, 200)};
  }
  if (op === 'lookupCancel') {
    const L = lookupForm();
    if (!L || !isCmd(L.fc.RequestCloseCancel)) return {fallback: 'no lookup to cancel'};
    call(L.fc, 'RequestCloseCancel');
    return {acted: 'RequestCloseCancel'};
  }
  if (op === 'lookupCommit') {
    const L = lookupForm();
    if (!L || !isCmd(L.fc.SelectionCompleted)) return {fallback: 'the lookup closed'};
    call(L.fc, 'SelectionCompleted');
    return {acted: 'SelectionCompleted'};
  }
  if (op === 'dirty') {  // a read: the visible forms whose data sources hold unsaved changes
    const out = [];
    for (const f of document.querySelectorAll('[data-dyn-form-name]')) {
      if (f.offsetParent === null) continue;
      let fc = null; try { fc = $dyn.context(f); } catch (e) { fc = null; }
      try { if (fc && typeof fc.AreDataSourcesDirty === 'function' && fc.AreDataSourcesDirty()) out.push(f.getAttribute('data-dyn-form-name')); } catch (e) { /* unreadable: not reported */ }
    }
    return {unsaved: Array.from(new Set(out))};
  }
  if (op === 'secretState') {  // a[0..2] x, y, control: D365 knows whether its password control holds a value
    const c = ctxAt(a[0], a[1], a[2]);
    return c && 'IsEmptyPassword' in c ? {empty: !!peek(c.IsEmptyPassword)} : {fallback: 'no password state'};
  }
  if (op === 'rawValue') {  // a[0..2] x, y, control: D365's own value (an ISO date), not the display text
    const c = ctxAt(a[0], a[1], a[2]);
    return c && 'Value' in c ? {value: String(peek(c.Value) ?? '')} : {fallback: 'no value'};
  }
  if (op === 'hasNextPage') {
    const el = gridIn(a[0], a[1]);
    let g = null; try { g = el && $dyn.context(el); } catch (e) { g = null; }
    return g && 'HasNextPage' in g ? {has_next: !!peek(g.HasNextPage), rows: ((g.DataSource && g.DataSource.__array) || []).length} : {fallback: 'no grid viewmodel'};
  }
  const c = ctxAt(a[0], a[1], a[2]);
  if (!c) return {fallback: 'no viewmodel at the control'};
  const type = c._typeName || '';
  if (op === 'set') {
    // A reference group's field (a worker's name for the worker reference): SetValue on it is not
    // resolved to a record and D365 restores the old text (live 10.0.49). Typed and left with Tab, D365
    // resolves the text as for a user (ResolveChanges).
    const hit = document.elementFromPoint(a[0], a[1]);
    if (hit && hit.closest('[data-dyn-role="ReferenceGroup"]')) return {fallback: 'reference group: typed for D365 to resolve'};
    const want = String(a[3]);
    if (type === 'ComboBox') {
      if (!isCmd(c.SetSelectedItem)) return {fallback: 'combo without SetSelectedItem'};
      const items = peek(c.Items) || [];
      const norm = s => String(s).trim().toLowerCase();
      const item = items.find(i => norm(i.Label) === norm(want)) || items.find(i => String(i.Value) === want.trim());
      if (!item) return {error: 'no_option', type, options: items.map(i => i.Label)};
      call(c, 'SetSelectedItem', [item.Value]);
      return {acted: 'SetSelectedItem', type, value: item.Label};
    }
    if (type === 'CheckBox') {
      if (!isCmd(c.toggleValue)) return {fallback: 'checkbox without toggleValue'};
      const v = peek(c.Value), on = v === true || ['1', 'true', 'yes'].includes(String(v).toLowerCase());
      if (on === (a[4] === true)) return {acted: 'none', type, unchanged: true};
      call(c, 'toggleValue');
      return {acted: 'toggleValue', type};
    }
    if (TEXT.has(type)) {
      if (!isCmd(c.SetValue)) return {fallback: type + ' without SetValue'};
      // Dates: SetValue takes ISO (2026-11-20) and ignores the user's display format (11/20/2026); a text
      // in the user's format is typed by the input path, as a user would.
      const iso = type === 'Date' ? /^\d{4}-\d{2}-\d{2}$/.test(want) : type === 'DateTime' ? /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/.test(want) : null;
      if (iso === false) return {fallback: type + ' in the display format: typed'};
      call(c, 'SetValue', [want]);
      return iso ? {acted: 'SetValue', type, raw: true} : {acted: 'SetValue', type};
    }
    return {fallback: 'no model setter for ' + (type || 'this control')};
  }
  if (op === 'press') {
    // A menu button's list opens on the client: its Clicked command shows nothing (live 10.0.49, the
    // attachments' New): the input path clicks it
    if (type === 'MenuButton') return {fallback: 'menu button: its list opens on a click'};
    if (isCmd(c.Clicked) && !/TabPage|SectionPage|PivotItem/.test(type)) { call(c, 'Clicked'); return {acted: 'Clicked', type}; }
    if (isCmd(c.ExpandedChanged)) { const open = !!Number(peek(c.Expanded)); call(c, 'ExpandedChanged', [!open]); return {acted: 'ExpandedChanged', type, expanded: !open}; }
    return {fallback: 'no model command for ' + (type || 'this control')};
  }
  return {error: 'unknown op'};
})"""


Js = Callable[..., Any]  # WebClient._js: the client's one route to the page
STALL_ROUNDS = 3  # LoadNextPage rounds without new rows before a read gives up asking


def set_value(js: Js, found: dict[str, Any], value: str, checked: bool | None) -> dict[str, Any] | None:
    """One model action for a located field. None: the model did not act (use input for this step).
    A dict with error `no_option` is D365's own answer (the combo has no such option) and must not fall
    back; any other answer without `acted` means nothing was sent, so input may act."""
    result = js("model:set", found["x"], found["y"], found["control"], value, checked)
    if isinstance(result, dict) and (result.get("acted") or result.get("error") == "no_option"):
        return result
    return None


def press(js: Js, found: dict[str, Any]) -> dict[str, Any] | None:
    """One model command for a located button or tab. None: the model did not act (use input)."""
    result = js("model:press", found["x"], found["y"], found["control"])
    return result if isinstance(result, dict) and result.get("acted") else None


def load_all_pages(js: Js, grid: str, wait_idle: Callable[[], Any], cap: int, more: Callable[[], bool],
                   form: str = "") -> dict[str, Any] | None:
    """Ask the server for the grid's remaining blocks (LoadNextPage) until none is left, `cap` rows are
    cached or more() says stop (the call's time). A read: it moves no selection and sends no input.
    None: the grid has no usable viewmodel."""
    loaded, rows, stalled = 0, -1, 0
    while more():
        step = js("model:loadPage", grid, form)
        if not isinstance(step, dict) or not step.get("acted"):
            return None if loaded == 0 else {"pages": loaded, "complete": False}
        if step["acted"] == "none":
            return {"pages": loaded, "complete": True, "rows": step.get("rows", 0)}
        loaded += 1
        wait_idle()
        after = js("model:hasNextPage", grid, form)
        if isinstance(after, dict) and not after.get("has_next"):
            return {"pages": loaded, "complete": True, "rows": after.get("rows", 0)}
        if isinstance(after, dict) and after.get("rows", 0) >= cap:
            return {"pages": loaded, "complete": False, "rows": after.get("rows", 0)}
        now = after.get("rows", 0) if isinstance(after, dict) else rows
        stalled = stalled + 1 if now <= rows else 0
        rows = max(rows, now)
        if stalled >= STALL_ROUNDS:  # LoadNextPage brings nothing (dropped, or the wrong grid): stop asking
            return {"pages": loaded, "complete": False, "rows": rows, "stalled": True}
    return {"pages": loaded, "complete": False}


def filter_grid(js: Js, value: str, column: str | None, form: str | None = None) -> dict[str, Any] | None:
    """The quick filter of the scope's form through its viewmodel (no click, no typing). None: no single
    usable viewmodel there (the input path then uses the scope's filter box)."""
    result = js("model:filter", value, column or "", form or "")
    if isinstance(result, dict) and (result.get("acted") or result.get("error") == "no_column"):
        return result
    return None


def open_lookup(js: Js, found: dict[str, Any]) -> bool:
    """Open the field's lookup through its viewmodel (no click, no Alt+Down)."""
    result = js("model:openLookup", found["x"], found["y"], found["control"])
    return isinstance(result, dict) and bool(result.get("acted"))


def pick_lookup_row(js: Js, pick: str, wait_idle: Callable[[], Any], cap: int, more: Callable[[], bool]) -> dict[str, Any]:
    """Select the open lookup's row holding `pick` and complete the selection (D365 transfers the value and
    closes the lookup): {"picked": True, "values": the row's text values}. Two rows holding `pick` are not
    guessed between ({}: the input path matches shown cells). Otherwise the lookup stays open: {"complete": True} when all its
    rows were searched, else {} (no viewmodel, the cap or the time ran out)."""
    rows, stalled = -1, 0
    while more():
        step = js("model:lookupPick", pick, cap)
        if not isinstance(step, dict):
            return {}
        if step.get("ambiguous"):
            return {"ambiguous": step["ambiguous"]}  # never guessed between, here or by the input path
        if step.get("loading"):
            stalled = stalled + 1 if step.get("rows", 0) <= rows else 0
            rows = max(rows, step.get("rows", 0))
            if stalled >= STALL_ROUNDS:
                return {}
            wait_idle()
            continue
        if not step.get("acted"):
            return {"complete": bool(step.get("complete"))}
        wait_idle()
        done = js("model:lookupCommit")
        if isinstance(done, dict) and done.get("acted"):
            return {"picked": True, "values": [str(v) for v in step.get("values") or []]}
        return {}
    return {}


def cancel_lookup(js: Js) -> bool:
    """Close the open lookup without a selection (its own cancel command, not Escape)."""
    result = js("model:lookupCancel")
    return isinstance(result, dict) and bool(result.get("acted"))


def unsaved_forms(js: Js, strict: bool = False) -> list[str] | None:
    """The visible forms with unsaved changes (D365's own AreDataSourcesDirty); [] when none. Unreadable:
    [] for the state report (it never fails a step); None with `strict`, for a guard that must not guess."""
    try:
        result = js("model:dirty")
    except Exception:
        return None if strict else []
    forms = result.get("unsaved") if isinstance(result, dict) else None
    if not isinstance(forms, list):
        return None if strict else []
    return [str(f) for f in forms]
