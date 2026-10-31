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
from .collect import Run, Runs, StillRunning  # noqa: F401 -- StillRunning: d365web's callers catch it here
from .d365web_browser import Browser, BrowserError, Page

PROFILE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
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
        if not PROFILE_NAME.match(self.profile):
            raise ValueError(f"profile must match {PROFILE_NAME.pattern}: {self.profile!r}")
        self.url = f"https://{parts.netloc}"

    def profile_dir(self) -> Path:
        return Path(self.profiles_dir) if self.profiles_dir else default_profiles_dir() / self.profile


# --- the page-side library --------------------------------------------------------------------------
# Called as (LIB)(op, args). Returns plain JSON. Kept free of D365 globals except $dyn.clientBusy.

LIB = r"""(function (op, a) {
  const GRID = '[data-dyn-role=ReactList], [data-dyn-role=Grid]';
  const TAB_ROLES = new Set(['AppBarTab', 'TabPage', 'SectionPage', 'Tab', 'PivotItem']);
  const TAB = Array.from(TAB_ROLES, r => '[data-dyn-role=' + r + ']').join(',');
  const FIELD_ROLES = new Set(['Input', 'Real', 'Integer', 'Int64', 'Date', 'DateTime', 'Time', 'ComboBox',
    'CheckBox', 'SegmentedEntry', 'MultilineInput', 'Guid', 'RadioButton', 'QuickFilter', 'StaticText']);
  const BUTTON_ROLES = new Set(['CommandButton', 'MenuItemButton', 'Button', 'DropDialogButton', 'MenuButton',
    'ToggleButton', 'MenuItem']);
  // an open menu button flyout ("Save and open" etc.) lives outside the form/dialog it belongs to
  const openMenus = () => Array.from(document.querySelectorAll('.flyoutButton-flyOut[role=menu], [role=menu].sysPopup'))
    .filter(e => vis(e) && e.querySelector('[data-dyn-controlname]'));
  const vis = (e) => {
    if (!e || !e.getBoundingClientRect) return false;
    const r = e.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const s = getComputedStyle(e);
    return s.visibility !== 'hidden' && s.display !== 'none';
  };
  const text = (e) => ((e && (e.innerText || e.textContent)) || '').replace(/\s+/g, ' ').trim();
  const cname = (e) => e.getAttribute('data-dyn-controlname') || '';
  const drole = (e) => e.getAttribute('data-dyn-role') || '';
  const area = (e) => { const r = e.getBoundingClientRect(); return r.width * r.height; };
  const byPos = (x, y) => { const p = x.getBoundingClientRect(), q = y.getBoundingClientRect();
    return Math.abs(p.top - q.top) > 4 ? p.top - q.top : p.left - q.left; };
  const inView = (e) => {
    const g = e.parentElement && e.parentElement.closest(GRID);
    if (!g) return true;
    const r = e.getBoundingClientRect(), b = g.getBoundingClientRect();
    return r.bottom > b.top + 1 && r.top < b.bottom - 1;
  };
  const inputOf = (e) => e.matches('input:not([type=checkbox]):not([type=radio]), textarea, select') ? e
    : e.querySelector('input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea, select');
  // a checkbox or a toggle (span.toggle-box[role=switch] drawn over a zero-size span[role=checkbox]):
  // click what is visible, read the state from either
  const CHECK = '[role=checkbox], [role=switch], input[type=checkbox]';
  const checkOf = (e) => {
    if (e.matches(CHECK)) return e;
    const all = Array.from(e.querySelectorAll(CHECK));
    return all.find(vis) || all[0] || null;
  };
  const isCheck = (e) => drole(e) === 'CheckBox' || (!!checkOf(e) && !inputOf(e));
  const checked = (e) => { const all = e.matches(CHECK) ? [e] : Array.from(e.querySelectorAll(CHECK));
    return all.some(c => c.getAttribute('aria-checked') === 'true' || c.checked === true); };
  const firstLine = (e) => ((e && (e.innerText || e.textContent)) || '').split('\n').map(x => x.trim()).filter(Boolean)[0] || '';
  // A tab contains content as well as its header. Never borrow a nested tab's header or
  // a link/button from the content; an unknown or ambiguous header must not dispatch a click.
  const tabHeader = (e) => {
    if (e.matches('button, [role=tab]')) return vis(e) ? e : null;
    if (drole(e) === 'PivotItem') {
      // Pivot headers are siblings of their panels. Follow the explicit reciprocal ARIA
      // association inside the same form/dialog, even when the inactive panel is hidden.
      const ids = (e.getAttribute('aria-labelledby') || '').trim().split(/\s+/).filter(Boolean);
      const owner = '[data-dyn-role=Form], [data-dyn-role=Popup], [role=dialog], [role=alertdialog]';
      const headers = ids.map(id => document.getElementById(id))
        .filter(h => h && h.matches('[role=tab]') && vis(h) && e.id &&
          h.getAttribute('aria-controls') === e.id && e.closest(owner) &&
          h.closest(owner) === e.closest(owner));
      return headers.length === 1 ? headers[0] : null;
    }
    const headers = Array.from(e.querySelectorAll('.appBarTab-header, .section-page-caption, [role=tab]'))
      .filter(h => h.closest(TAB) === e && vis(h));
    if (headers.length) return headers.length === 1 ? headers[0] : null;
    const direct = Array.from(e.children)
      .filter(h => h.matches('button[aria-expanded], [data-dyn-role=TabPageHeader], [data-dyn-role=SectionPageHeader]'))
      .map(h => h.matches('button') ? h : (h.querySelector('button, [role=tab]') || h)).filter(vis);
    return direct.length === 1 ? direct[0] : null;
  };
  const controlVisible = (e) => vis(e) || (drole(e) === 'PivotItem' && !!tabHeader(e));
  const labelOf = (e) => {
    const role = drole(e);
    if (TAB_ROLES.has(role)) return e.getAttribute('aria-label') || firstLine(tabHeader(e));
    const l = e.querySelector('label');
    const i = inputOf(e);
    return (l && text(l)) || e.getAttribute('aria-label') || (i && i.getAttribute('aria-label')) ||
      e.getAttribute('title') || (BUTTON_ROLES.has(drole(e)) || drole(e) === 'AppBarTab' ? text(e) : '') || '';
  };
  const valueOf = (e) => {
    if (isCheck(e)) return checked(e) ? 'Yes' : 'No';
    const i = inputOf(e);
    return i ? (i.value || '') : text(e);
  };
  const readOnly = (e) => {
    if (isCheck(e)) { const c = checkOf(e); return !!c && (c.getAttribute('aria-readonly') === 'true' || c.disabled === true || c.getAttribute('aria-disabled') === 'true'); }
    const i = inputOf(e);
    return !i || i.readOnly || i.disabled || i.getAttribute('aria-readonly') === 'true';
  };
  const enabled = (e) => {
    const b = TAB_ROLES.has(drole(e)) ? tabHeader(e) : (e.matches('button') ? e : (e.querySelector('button') || e));
    return !!b && !(b.disabled || e.getAttribute('aria-disabled') === 'true' || b.getAttribute('aria-disabled') === 'true' ||
      e.classList.contains('disabled'));
  };
  const isLink = (c) => !!c.querySelector('a, [role=link], [class*=yperlink]');

  function scope() {
    const pops = Array.from(document.querySelectorAll('[data-dyn-role=Popup]')).filter(vis);
    if (pops.length) { const el = pops[pops.length - 1];
      return {el, kind: el.classList.contains('lookup-popup') ? 'lookup' : 'popup'}; }
    // a hover preview of a linked record (form *_AutoEnhancedPreview_*) is not a dialog to work in
    const dialogs = Array.from(document.querySelectorAll('[role=dialog], [role=alertdialog]')).filter(vis)
      .filter(d => !/EnhancedPreview/i.test(formName(d)));
    if (dialogs.length) return {el: dialogs[dialogs.length - 1], kind: 'dialog'};
    const forms = Array.from(document.querySelectorAll('[data-dyn-role=Form]')).filter(vis);
    if (forms.length) return {el: forms.reduce((x, y) => area(y) > area(x) ? y : x), kind: 'form'};
    return {el: document.body, kind: 'page'};
  }
  const formName = (el) => el.getAttribute('data-dyn-form-name') || el.getAttribute('data-dyn-formname') ||
    ((el.querySelector('[data-dyn-form-name]') || {getAttribute: () => ''}).getAttribute('data-dyn-form-name')) || '';

  // D365 moves an unpinned tab's content under BODY. Its flyout retains the tab's ID plus
  // "_flyout"; only an expanded tab inside this scope may contribute that detached root.
  function scopeRoots(root) {
    const roots = [root];
    for (const tab of root.querySelectorAll('[data-dyn-role=AppBarTab]')) {
      const head = tabHeader(tab);
      if (!tab.id || !vis(tab) || !head || head.getAttribute('aria-expanded') !== 'true') continue;
      const flyout = document.getElementById(tab.id + '_flyout');
      if (flyout && flyout.classList.contains('appBar-flyout') && vis(flyout) &&
          flyout.getAttribute('aria-hidden') !== 'true' && !root.contains(flyout)) roots.push(flyout);
    }
    return roots;
  }
  const queryScope = (root, selector) => Array.from(new Set(scopeRoots(root)
    .flatMap(r => Array.from(r.querySelectorAll(selector)))));

  function messages() {
    const out = [], seen = new Set();
    for (const s of document.querySelectorAll('[data-dyn-role=MessageBarSection]')) {
      if (!vis(s)) continue;
      const m = /messageBarSection-(\w+)/.exec(s.className.toString());
      const severity = m ? m[1].toLowerCase() : 'info';
      const region = s.querySelector('.messageBar-messageRegion') || s;
      const items = Array.from(region.querySelectorAll('li')).map(text).filter(Boolean);
      for (const t of (items.length ? items : [text(region)])) {
        if (!t || seen.has(severity + t)) continue;
        seen.add(severity + t);
        out.push({severity, text: t.slice(0, 8000)});
      }
    }
    return out;
  }

  function gridRows(g) {
    const rows = Array.from(g.querySelectorAll('[role=row]'));
    let header = null; const body = [];
    for (const r of rows) {
      const isHeader = !!r.querySelector('[role=columnheader]') || (!header && !body.length && !r.querySelector('input'));
      if (isHeader) { if (!header) header = r; continue; }
      if (vis(r) && inView(r)) body.push(r);
    }
    body.sort(byPos);
    return {header, body};
  }
  function cellsOf(r) {
    return Array.from(r.querySelectorAll('[role=gridcell], [role=columnheader]')).filter(vis);
  }
  function cellName(c) { const n = c.matches('[data-dyn-controlname]') ? c : c.querySelector('[data-dyn-controlname]'); return n ? cname(n) : ''; }
  function cellValue(c) { const n = c.querySelector('[data-dyn-controlname]') || c; return valueOf(n); }
  function readGrid(g, maxRows) {
    const {header, body} = gridRows(g);
    let columns = header ? cellsOf(header).map(c => ({name: cellName(c), label: text(c)})) : [];
    if (!columns.length && body.length) columns = cellsOf(body[0]).map(c => ({name: cellName(c), label: ''}));
    const keep = columns.map(c => !!(c.name || c.label));
    const rows = body.slice(0, maxRows).map((r, i) => {
      const cells = cellsOf(r);
      const values = columns.map((col, k) => {
        const c = cells.find(x => col.name && cellName(x) === col.name) || cells[k];
        return c ? cellValue(c) : '';
      }).filter((v, k) => keep[k]);
      const row = {row: i, values};
      const key = r.getAttribute('aria-rowindex') || r.getAttribute('data-dyn-row');
      if (key) row.key = key;
      if (r.getAttribute('aria-selected') === 'true' || /selected|activeRow/i.test(r.className.toString())) row.selected = true;
      return row;
    });
    const total = g.getAttribute('aria-rowcount') || (g.querySelector('[aria-rowcount]') || {getAttribute: () => null}).getAttribute('aria-rowcount');
    const out = {name: cname(g), columns: columns.filter((c, k) => keep[k]), rows, visible_rows: body.length};
    if (total) out.rowcount_hint = Number(total);
    return out;
  }
  // D365 grids render only the rows near the visible area: scrolling their container (like a person)
  // brings the next rows into the DOM. The scroller is the grid's tallest scrollable descendant.
  function scrollerOf(g) {
    let best = null;
    for (const e of [g, ...g.querySelectorAll('*')]) {
      if (e.scrollHeight <= e.clientHeight + 4) continue;
      const oy = getComputedStyle(e).overflowY;
      if ((oy === 'auto' || oy === 'scroll') && (!best || e.clientHeight > best.clientHeight)) best = e;
    }
    return best;
  }
  function findGrid(root, name) {
    const grids = queryScope(root, GRID).filter(vis);
    return name ? grids.find(g => cname(g) === name) : grids.sort((x, y) => area(y) - area(x))[0];
  }

  function controls(root, full) {
    const fieldsOut = [], buttons = [], actionTabs = [], tabs = [], other = [];
    const count = {};
    const roots = scopeRoots(root);
    const all = queryScope(root, '[data-dyn-controlname]').filter(e => controlVisible(e) && inView(e));
    all.sort(byPos);
    for (const e of all) {
      const g = e.parentElement && e.parentElement.closest(GRID);
      if (g && roots.some(r => r.contains(g))) continue;
      const role = drole(e), name = cname(e);
      if (!name) continue;
      const nth = count[name] = (count[name] === undefined ? 0 : count[name] + 1);
      const item = {name};
      if (nth) item.nth = nth;
      const label = labelOf(e).slice(0, 60);
      if (label && label !== name) item.label = label;
      if (FIELD_ROLES.has(role) && (inputOf(e) || isCheck(e) || role === 'StaticText')) {
        item.value = valueOf(e).slice(0, 200);
        if (readOnly(e)) item.readonly = true;
        if (e.querySelector('.statusMandatory') || e.classList.contains('statusMandatory')) item.required = true;
        if (role !== 'Input') item.role = role;
        fieldsOut.push(item);
      } else if (BUTTON_ROLES.has(role)) {
        if (!enabled(e)) item.disabled = true;
        buttons.push(item);
      } else if (role === 'AppBarTab') {
        actionTabs.push(item);
      } else if (TAB_ROLES.has(role)) {
        const head = tabHeader(e);
        const ex = e.getAttribute('aria-expanded') || (head && head.getAttribute('aria-expanded'));
        if (ex !== null) item.expanded = ex === 'true';
        if (head && head.hasAttribute('aria-selected')) item.selected = head.getAttribute('aria-selected') === 'true';
        tabs.push(item);
      } else if (full) {
        item.role = role || e.getAttribute('role') || e.tagName.toLowerCase();
        const t = text(e).slice(0, 60); if (t) item.text = t;
        other.push(item);
      }
    }
    if (full) return {fields: fieldsOut, buttons, action_tabs: actionTabs, tabs, other};
    // compact: name[#nth] -> value; labels only in full mode (press/set also accept labels)
    const key = (i) => i.nth ? i.name + '[' + i.nth + ']' : i.name;
    // filter-pane operator buttons carry generated numeric names: noise in the compact view
    const named = buttons.filter(b => !/^\d+$/.test(b.name));
    const out = {fields: {}, readonly: {}, buttons: named.filter(b => !b.disabled).map(key),
      disabled: named.filter(b => b.disabled).map(key),
      action_tabs: actionTabs.map(t => t.label && t.label !== t.name ? t.name + ' (' + t.label + ')' : t.name),
      tabs: tabs.map(t => key(t) + (t.label && t.label !== t.name ? ' (' + t.label + ')' : '') + (t.expanded === false ? ' [collapsed]' : ''))};
    for (const f of fieldsOut) (f.readonly ? out.readonly : out.fields)[key(f)] = f.value;
    // labels only where the name does not already say it (Proforma -> Posting), to keep the view small
    const norm = (t) => (t || '').toLowerCase().replace(/[^a-z0-9]/g, '');
    const labels = {};
    for (const i of fieldsOut.concat(named)) {
      if (i.label && !norm(i.name).includes(norm(i.label)) && Object.keys(labels).length < 80) labels[key(i)] = i.label;
    }
    if (Object.keys(labels).length) out.labels = labels;
    const missing = fieldsOut.filter(f => f.required && !f.readonly && !f.value).map(key);
    if (missing.length) out.missing = missing;
    return out;
  }

  function state(full, maxRows) {
    const sc = scope();
    const out = {scope: sc.kind, form: formName(sc.el), title: document.title.split(' -- ')[0],
      busy: typeof $dyn !== 'undefined' && $dyn.clientBusy ? !!$dyn.clientBusy() : null, messages: messages()};
    const cap = Array.from(sc.el.querySelectorAll('.titleField')).filter(vis)[0];
    if (cap && text(cap)) out.caption = text(cap).slice(0, 200);
    if (sc.kind === 'dialog' || sc.kind === 'popup') out.dialog_text = text(sc.el).slice(0, 400);
    Object.assign(out, controls(sc.el, full));
    out.grids = queryScope(sc.el, GRID).filter(vis).map(g => readGrid(g, maxRows));
    const menu = [];
    for (const m of openMenus()) for (const e of m.querySelectorAll('[data-dyn-controlname]')) {
      if (!vis(e)) continue;
      const n = cname(e), l = labelOf(e) || text(e);
      menu.push(l && l !== n ? n + ' (' + l + ')' : n);
    }
    if (menu.length) out.menu = menu;
    const counter = document.querySelector('.messageCenter-counter');
    if (counter && text(counter) && text(counter) !== '0') out.notifications = Number(text(counter)) || text(counter);
    return out;
  }

  function similar(root, name) {
    const want = name.toLowerCase().replace(/[^a-z0-9]/g, '');
    const out = new Set();
    for (const e of queryScope(root, '[data-dyn-controlname]')) {
      if (!vis(e)) continue;
      const n = cname(e), l = labelOf(e);
      const k = (n + ' ' + l).toLowerCase().replace(/[^a-z0-9 ]/g, '');
      if (want && k.replace(/ /g, '').includes(want)) out.add(l && l !== n ? n + ' (' + l + ')' : n);
      if (out.size >= 12) break;
    }
    return Array.from(out);
  }

  function candidates(root, name, kind) {
    const all = queryScope(root, '[data-dyn-controlname]');
    let c = all.filter(e => cname(e) === name);
    const visible = kind === 'field' ? vis : controlVisible;
    if (!c.filter(visible).length) {
      const want = name.trim().toLowerCase();
      c = all.filter(e => labelOf(e).trim().toLowerCase() === want);
    }
    const hidden = c.length;
    c = c.filter(e => visible(e) && inView(e) && !e.closest('[role=columnheader]'));
    if (kind === 'field') c = c.filter(e => !TAB_ROLES.has(drole(e)) && (inputOf(e) || isCheck(e)));
    c = c.filter(e => !c.some(o => o !== e && e.contains(o)));
    c.sort(byPos);
    return {list: c, hidden: hidden - c.length};
  }

  function locate(name, nth, kind) {
    const sc = scope();
    let {list, hidden} = candidates(sc.el, name, kind);
    if (kind !== 'field') {  // an open menu's items take precedence over the scope's buttons
      for (const m of openMenus()) {
        const hit = candidates(m, name, kind);
        if (hit.list.length) { list = hit.list; hidden = 0; break; }
      }
    }
    if (!list[nth]) return {error: 'not_found', scope: sc.kind, form: formName(sc.el), count: list.length,
      hidden_matches: hidden, similar: similar(sc.el, name)};
    const e = list[nth], tab = kind !== 'field' && TAB_ROLES.has(drole(e));
    const head = tab ? tabHeader(e) : null;
    if (tab && !head) return {error: 'not_found', reason: 'tab_header_unavailable', control: cname(e),
      role: drole(e), scope: sc.kind, form: formName(sc.el)};
    let target = e;
    // a hyperlink (grid cells: input.dyn-hyperlink) reacts only on its text, which starts at the left
    const link = kind === 'field' || tab ? null : (e.matches('.dyn-hyperlink, a') ? e : e.querySelector('.dyn-hyperlink, a'));
    if (kind === 'field') target = isCheck(e) ? (checkOf(e) || e) : inputOf(e);
    else if (tab) target = head;
    else if (link) target = link;
    else if (!e.matches('button')) target = e.querySelector('button') || e;
    target.scrollIntoView({block: 'nearest', inline: 'nearest'});
    const r = target.getBoundingClientRect();
    const x = link ? r.left + Math.min(14, r.width / 2) : r.left + r.width / 2;
    const out = {x, y: r.top + r.height / 2, count: list.length, role: drole(e),
      control: cname(e), scope: sc.kind};
    // what a click at (x, y) would really hit: a teaching tip or other popup can lie over the control
    const hit = document.elementFromPoint(x, out.y);
    if (hit && !(tab ? target.contains(hit) : (e.contains(hit) || target.contains(hit) || hit.contains(target)))) {
      const tip = hit.closest('.qtip');
      out.covered = {by: ((hit.closest('[data-dyn-controlname]') && cname(hit.closest('[data-dyn-controlname]'))) ||
        (hit.className || '').toString().slice(0, 60) || hit.tagName), tip: tip ? tip.id : '',
        text: firstLine(tip || hit).slice(0, 80)};
    }
    if (kind === 'field') { out.check = isCheck(e); out.value = valueOf(e); out.readonly = readOnly(e); }
    else out.enabled = enabled(e);
    return out;
  }

  function rowTarget(gridName, index, match) {
    const sc = scope();
    const g = findGrid(sc.el, gridName);
    if (!g) return {error: 'not_found', what: 'grid', scope: sc.kind,
      grids: Array.from(sc.el.querySelectorAll(GRID)).filter(vis).map(cname)};
    const {header, body} = gridRows(g);
    const labels = header ? cellsOf(header).map(c => [cellName(c), text(c).toLowerCase()]) : [];
    let row = null;
    if (match) {
      const keys = Object.keys(match);
      row = body.find(r => keys.every(k => {
        const want = String(match[k]).trim().toLowerCase();
        const cells = cellsOf(r);
        if (k === '*') return cells.some(c => cellValue(c).trim().toLowerCase() === want);
        let c = cells.find(x => cellName(x) === k);
        if (!c) { const i = labels.findIndex(l => l[1] === k.toLowerCase()); if (i >= 0) c = cells.find(x => cellName(x) === labels[i][0]) || cells[i]; }
        return !!c && cellValue(c).trim().toLowerCase() === want;
      }));
    } else row = body[index];
    if (!row) return {error: 'not_found', what: 'row', grid: cname(g), visible_rows: body.length};
    const cells = cellsOf(row).filter(c => cellName(c));
    const plain = cells.find(c => !isLink(c));
    if (!plain && cells.length) {
      // only hyperlink cells (a list pane with one linked column, e.g. the menu items of a setup form):
      // their text opens the record, it does not select the row (test box, 2026-10-26: the next Delete
      // landed on the opened record). Click a point of the row that no link covers, or refuse.
      row.scrollIntoView({block: 'nearest', inline: 'nearest'});
      const rr = row.getBoundingClientRect(), y = rr.top + rr.height / 2;
      const linkAt = (e) => !!(e && e.closest('a, [role=link], [class*=yperlink]'));
      for (let x = rr.right - 6; x > rr.left + 4; x -= 12) {
        const hit = document.elementFromPoint(x, y);
        if (hit && row.contains(hit) && !linkAt(hit)) return {x, y, grid: cname(g), row: body.indexOf(row), cell: ''};
      }
      return {error: 'links_only', grid: cname(g), row: body.indexOf(row), cells: cells.map(cellName)};
    }
    const cell = plain || row;
    cell.scrollIntoView({block: 'nearest', inline: 'nearest'});
    const r = cell.getBoundingClientRect();
    return {x: r.left + Math.min(r.width / 2, 40), y: r.top + r.height / 2, grid: cname(g), row: body.indexOf(row),
      cell: cellName(cell)};
  }

  function status() {
    if (/(^|\.)login\.(microsoftonline|live|windows)\.(com|net)$/.test(location.hostname)) return 'signin';
    if (location.href === 'about:blank') return 'idle';
    if (document.readyState !== 'complete') return 'busy';
    if (typeof $dyn === 'undefined' || !$dyn.clientBusy) return 'busy';
    if ($dyn.clientBusy()) return 'busy';
    // "Processing operation" (a SysBoxForm lightbox with div.progressBar[role=progressbar]) can outlast
    // clientBusy(); while it runs the main area also carries aria-busy (captured 2026-10-23, journal post)
    if (Array.from(document.querySelectorAll('.primaryConductor[aria-busy=true]')).some(vis)) return 'busy';
    const progress = Array.from(document.querySelectorAll('[role=dialog]')).filter(vis)
      .some(d => d.querySelector('[role=progressbar], [data-dyn-role=ProgressBar]'));
    return progress ? 'busy' : 'idle';
  }

  switch (op) {
    case 'status': return status();
    case 'state': return state(!!a[0], a[1] || 20);
    case 'locate': return locate(a[0], a[1] || 0, a[2] || 'any');
    case 'grid': { const sc = scope(); const g = findGrid(sc.el, a[0]);
      return g ? readGrid(g, a[1] || 50) : {error: 'not_found', what: 'grid', scope: sc.kind,
        grids: Array.from(sc.el.querySelectorAll(GRID)).filter(vis).map(cname)}; }
    case 'row': return rowTarget(a[0] || '', a[1] || 0, a[2] || null);
    case 'gridScroll': { const sc = scope(); const g = findGrid(sc.el, a[0]);
      if (!g) return {error: 'not_found', what: 'grid', scope: sc.kind};
      const s = scrollerOf(g);
      if (!s) return {scrollable: false};
      const before = s.scrollTop;
      s.scrollTop = a[1] === 'top' ? 0 : before + Math.max(24, Math.floor(s.clientHeight * 0.8));
      s.dispatchEvent(new Event('scroll'));
      return {scrollable: true, before, after: s.scrollTop, height: s.scrollHeight, client: s.clientHeight}; }
    case 'scope': { const sc = scope(); return {scope: sc.kind, form: formName(sc.el)}; }
    case 'tipButton': { const q = document.getElementById(a[0]);
      if (!q || !vis(q)) return {error: 'gone'};
      const b = Array.from(q.querySelectorAll('button, [role=button], a')).filter(vis)[0];
      if (!b) return {error: 'no_button'};
      const r = b.getBoundingClientRect();
      return {x: r.left + r.width / 2, y: r.top + r.height / 2, label: firstLine(b).slice(0, 40),
        text: firstLine(q).slice(0, 80)}; }
    case 'restPoint': {  // somewhere inert in the current form for the pointer to rest (no link, button, row, tooltip)
      const sc = scope(), r = sc.el.getBoundingClientRect();
      const busy = (e) => !e || !!e.closest('a, button, input, textarea, [role=row], [role=link], [role=button], ' +
        '[role=gridcell], [role=menuitem], [title], [data-dyn-role$=Button], [class*=yperlink]');
      const xs = [0.98, 0.9, 0.75, 0.5], ys = [0.98, 0.9, 0.75, 0.5];
      for (const fy of ys) for (const fx of xs) {
        const x = r.left + (r.width - 2) * fx, y = r.top + (r.height - 2) * fy;
        if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
        if (!busy(document.elementFromPoint(x, y))) return {x, y};
      }
      return {error: 'none'}; }
    case 'messages': return messages();
    case 'labels': { const sc = scope(); const out = {};
      for (const e of queryScope(sc.el, '[data-dyn-controlname]')) {
        if (!vis(e)) continue;
        const l = labelOf(e); if (l && !(cname(e) in out)) out[cname(e)] = l.slice(0, 80); }
      return out; }
    case 'comboOption': {
      const opts = Array.from(document.querySelectorAll('.comboBox-list li[role=option], [role=listbox].comboBox-list [role=option]')).filter(vis);
      const names = opts.map(o => text(o));
      const k = names.findIndex(n => n.toLowerCase() === String(a[0]).trim().toLowerCase());
      if (k < 0) return {error: 'not_found', options: names};
      // a long list scrolls in its popup: an option below the fold is clipped and its centre lies
      // outside the visible list, so the click would miss it (Activity code, test box, 2026-10-26)
      opts[k].scrollIntoView({block: 'nearest'});
      const r = opts[k].getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
      const hit = document.elementFromPoint(x, y);
      if (!hit || !(hit === opts[k] || opts[k].contains(hit))) return {error: 'covered', options: names};
      return {x, y}; }
    case 'quickfilter': { const sc = scope();
      const qf = Array.from(sc.el.querySelectorAll('[data-dyn-role=QuickFilter]')).filter(vis)[0];
      const i = qf && qf.querySelector('input');
      if (!i) return {error: 'not_found', what: 'quick filter', scope: sc.kind, form: formName(sc.el)};
      const r = i.getBoundingClientRect(); return {x: r.left + r.width / 2, y: r.top + r.height / 2}; }
    case 'filteroption': {
      const opts = Array.from(document.querySelectorAll('.quickFilter-dropdown li[role=option]')).filter(vis);
      const names = opts.map(o => text(o.querySelector('.quickFilter-listFieldName') || o));
      const want = (a[0] || '').trim().toLowerCase();
      const k = want ? names.findIndex(n => n.toLowerCase() === want) : 0;
      if (k < 0 || !opts[k]) return {error: 'not_found', what: 'filter column', columns: names};
      const r = opts[k].getBoundingClientRect(); return {x: r.left + r.width / 2, y: r.top + r.height / 2, column: names[k]}; }
    case 'whsform': {  // the in-form WHS app simulator (action:WHSWorkExecute): its controls by protocol name
      const f = Array.from(document.querySelectorAll('[data-dyn-form-name=WHSWorkExecute]')).filter(vis).pop();
      if (!f) return null;
      const hdr = f.querySelector('.dialog-caption');
      const skip = /^(SystemDefined|\$Dialog|NonProductionNotification|MinWidth)/;
      const controls = [];
      for (const e of f.querySelectorAll('[data-dyn-controlname]')) {
        const name = cname(e), role = e.getAttribute('data-dyn-role');
        if (skip.test(name) || role === 'Group' || !vis(e)) continue;
        const c = {name, role};
        const inp = e.querySelector('input, textarea');
        if (inp && role !== 'CommandButton' && role !== 'Button') {
          c.value = inp.type === 'password' ? '' : inp.value;
          if (inp.readOnly || inp.disabled) c.readonly = true;
          const l = labelOf(e); if (l) c.label = l;
        } else c.text = text(e);
        if (name === 'error') { const cs = getComputedStyle(e); c.bg = cs.backgroundColor; c.fg = cs.color; }
        controls.push(c);
      }
      return {caption: hdr ? text(hdr) : '', controls};
    }
  }
  return {error: 'unknown op ' + op};
})"""


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


def _number(text: str) -> float | None:
    try:
        return float(text.replace(",", "").strip())
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
    return not b.lower().startswith(a.lower())


SCENARIO_STEPS_MAX = 80
STEP_ARGS = {
    "open": {"menu_item", "company", "kind"}, "state": {"full", "max_rows"}, "set": {"values"},
    "press": {"name", "nth"}, "lookup": {"field", "pick", "nth"}, "filter": {"value", "column"},
    "select_row": {"grid", "row", "match"}, "key": {"keys"},
}
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
                "key": "keys"}
    if op in required and required[op] not in args:
        raise D365WebError("refused", f"{op}: missing {required[op]}")
    for name in ("menu_item", "name", "field", "keys"):
        if name in args:
            _step_text(args[name], name)
    for name in ("company", "kind", "pick", "column", "grid"):
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
        if kind is not None and kind not in ("display", "action", "output"):
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
        found = _grid_rows(state, spec.get("grid"))
        if found:
            columns, rows = found
            index = next((i for i, c in enumerate(columns) if c.get("name") == source[5:]
                          or (c.get("label") or "").lower() == source[5:].lower()), None)
            row = int(spec.get("row", 0))
            if index is not None and row < len(rows):
                text = rows[row]["values"][index]
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
        self.dismissed: list[str] = []  # teaching tips acknowledged during the current step (see _uncover)
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
                    self.executor.submit(self._close_browser)

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
            except (OSError, ValueError, KeyError, BrowserError):
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
            self.page = Page(self.browser.conn, pages[0]["targetId"]) if pages else self.browser.new_page()
        return self.page

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
        return page.evaluate(f"({LIB})({json.dumps(op)}, {json.dumps(list(args))})")

    def _accept_js_dialogs(self) -> None:
        page = self.page
        if page is None:
            return
        with page.events_lock:
            dialogs, page.dialogs = page.dialogs, []
        for dialog in dialogs:
            self.js_dialogs.append(f"{dialog.get('type', '')}: {dialog.get('message', '')}"[:300])
            try:
                page.send("Page.handleJavaScriptDialog", {"accept": True})
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

    def _state(self, full: bool = False, max_rows: int = 20, idle: bool = True) -> dict[str, Any]:
        page = self._ensure()
        result: dict[str, Any] = {}
        result.update(self._js("state", full, max_rows))
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

    def _locate(self, name: str, nth: int, kind: str) -> dict[str, Any]:
        found = self._js("locate", name, nth, kind)
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
            found = self._uncover(name, nth, kind, found)
        return found

    def _uncover(self, name: str, nth: int, kind: str, found: dict[str, Any]) -> dict[str, Any]:
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
            found = self._js("locate", name, nth, kind)
            if found.get("error"):
                raise D365WebError("not_found", f"{name!r} is gone after dismissing a tip", **found)
            if not found.get("covered"):
                return found
            cover = found["covered"]
        raise D365WebError("covered", f"{name!r} is covered by {cover.get('by')!r}"
                           f"{(': ' + cover['text']) if cover.get('text') else ''}; nothing was clicked",
                           covered=cover)

    # --- operations (worker thread only) ----------------------------------------------------------
    def _op_open(self, menu_item: str, company: str | None, kind: str | None) -> dict[str, Any]:
        page = self._ensure()
        mi = f"{kind}:{menu_item}" if kind and kind != "display" else menu_item
        if not MENU_ITEM.match(mi):
            raise D365WebError("refused", f"bad menu item {mi!r}")
        cmp = (company or self.config.company).lower()
        if cmp and not re.match(r"^[a-z0-9]{1,8}$", cmp):
            raise D365WebError("refused", f"bad company {cmp!r}")
        query = f"cmp={quote(cmp)}&mi={quote(mi)}" if cmp else f"mi={quote(mi)}"  # no cmp: the user's default
        page.navigate(f"{self.config.url}/?{query}")
        time.sleep(0.5)
        return self._after_step([])

    def _op_set(self, values: list[tuple[str, Any]]) -> dict[str, Any]:
        page = self._ensure()
        before = self._js("messages")
        applied: list[dict[str, Any]] = []
        for key, raw in values:
            name, nth = parse_field_key(key)
            value = "" if raw is None else str(raw)
            found = self._locate(name, nth, "field")
            deadline = time.monotonic() + READONLY_GRACE
            while found.get("readonly") and time.monotonic() < deadline:  # enabled by an earlier field?
                time.sleep(0.25)
                found = self._locate(name, nth, "field")
            if found.get("readonly"):
                raise D365WebError("refused", f"field {name!r} is read-only here", applied=list(applied))
            if found.get("check"):
                if as_bool(value) != (found.get("value") == "Yes"):
                    page.click(found["x"], found["y"])
                    self._wait_idle()
                    again = self._js("locate", name, nth, "field")
                    if not again.get("error") and (again.get("value") == "Yes") != as_bool(value):
                        # the click was swallowed (a re-query, a popup): once more, through _locate's checks
                        again = self._locate(name, nth, "field")
                        if (again.get("value") == "Yes") != as_bool(value):
                            page.click(again["x"], again["y"])
            elif found.get("role") == "ComboBox":  # an enum: pick from its list, typing is ignored
                if value.strip().lower() != str(found.get("value", "")).strip().lower():
                    self._pick_combo(page, found, name, value, applied)
            else:
                page.click(found["x"], found["y"])
                time.sleep(0.15)
                page.key("Ctrl+a")
                if value:
                    page.type_text(value)
                else:
                    page.key("Delete")
                page.key("Tab")
            self._wait_idle()
            now = self._js("locate", name, nth, "field")
            line = {"field": key, "value": value}
            if not now.get("error"):
                line["now"] = now.get("value", "")
                if found.get("check"):
                    if (line["now"] == "Yes") != as_bool(value):
                        line["differs"] = True  # the checkbox did not take the value, even after a retry
                elif value_differs(value, line["now"]):
                    line["differs"] = True  # D365 changed or silently dropped the value
            applied.append(line)
            # D365 reports rejected values as warnings ("... is not found in the related table")
            rejected = [m for m in self._js("messages") if m["severity"] in ("error", "warning") and m not in before]
            if rejected:
                return self._after_step(before, applied=applied, stopped_at=key)
        return self._after_step(before, applied=applied)

    def _pick_combo(self, page: Page, found: dict[str, Any], name: str, value: str,
                    applied: list[dict[str, Any]]) -> None:
        page.click(found["x"], found["y"])
        time.sleep(0.15)
        page.key("Alt+ArrowDown")
        option: dict[str, Any] = {}
        for _ in range(12):
            time.sleep(0.25)
            option = self._js("comboOption", value)
            if not option.get("error") or option.get("options"):
                break
        if option.get("error"):
            page.key("Escape")
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
        if found.get("enabled") is False:
            raise D365WebError("refused", f"{name!r} is disabled")
        page.click(found["x"], found["y"])
        time.sleep(0.2)
        return self._after_step(before, pressed=found.get("control", name))

    def _op_key(self, keys: str) -> dict[str, Any]:
        page = self._ensure()
        before = self._js("messages")
        for combo in keys.split():
            page.key(combo)
            time.sleep(0.1)
        return self._after_step(before)

    def _op_lookup(self, name: str, nth: int, pick: str | None) -> dict[str, Any]:
        page = self._ensure()
        before = self._js("messages")
        if self._js("scope")["scope"] != "lookup":
            self._open_lookup(page, name, nth, None)
        if pick is None:
            return self._after_step(before)
        target = self._js("row", "", 0, {"*": pick})
        if target.get("error"):
            page.key("Escape")
            self._wait_idle()
            self._open_lookup(page, name, nth, pick)  # typed text filters the lookup
            target = self._js("row", "", 0, {"*": pick})
            if target.get("error"):
                rows = self._js("grid", "", 10)
                page.key("Escape")
                raise D365WebError("not_found", f"no lookup row with a value {pick!r}", lookup=rows)
        page.click(target["x"], target["y"])
        return self._after_step(before, picked=pick)

    def _open_lookup(self, page: Page, name: str, nth: int, typed: str | None) -> None:
        found = self._locate(name, nth, "field")
        page.click(found["x"], found["y"])
        time.sleep(0.15)
        if typed is not None:
            page.key("Ctrl+a")
            page.type_text(typed)
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
            return self._op_lookup(args["field"], int(args.get("nth", 0)), args.get("pick"))
        if op == "filter":
            return self._op_filter(str(args.get("value", "")), args.get("column"))
        if op == "select_row":
            return self._op_select_row(args.get("grid", ""), int(args.get("row", 0)), args.get("match"))
        if op == "key":
            return self._op_key(args["keys"])
        raise D365WebError("refused", f"unknown op {op!r}")

    def _op_filter(self, value: str, column: str | None) -> dict[str, Any]:
        page = self._ensure()
        before = self._js("messages")
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
            page.key("Escape")
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
        complete, still = not top.get("scrollable"), 0
        columns = first.get("columns", [])
        while not complete and len(rows) < GRID_SCAN_CAP and not (stop and stop(list(rows.values()), columns)):
            moved = self._js("gridScroll", grid, "down")
            self._wait_idle()
            gained = add(self._js("grid", grid, 200))
            if moved.get("after", 0) <= moved.get("before", 0) and not gained:
                still += 1  # at the bottom; D365 may still be loading the next block, so look once more
                if still >= 2:
                    complete = True
            else:
                still = 0
        hint = first.get("rowcount_hint") or 0  # aria-rowcount, the header row included
        if complete and hint and len(rows) < hint - 1 and not (stop and stop(list(rows.values()), columns)):
            # Some grids keep one data block in the DOM, all of it in view, and fetch the next block only
            # when the selection moves past its last row with the keyboard (a custom receiving log on
            # the test box, 2026-10-26: 30 of 43 rows). Select the last rendered row, then page down like a person.
            # This moves the grid's selection, which a scroll does not.
            last = self._js("row", grid, max(0, len(self._js("grid", grid, 200).get("rows", [])) - 1), None)
            if not last.get("error"):
                page = self._ensure()
                page.click(last["x"], last["y"])
                rest = self._js("restPoint")
                if not rest.get("error"):
                    page.move(rest["x"], rest["y"])
                self._wait_idle()
                still = 0
                while not (stop and stop(list(rows.values()), columns)):  # also after the last block came
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
                complete = len(rows) >= hint - 1
        listed = [dict(row, row=i) for i, row in enumerate(rows.values())]
        return {"name": first.get("name"), "columns": first.get("columns", []), "rows": listed,
                "complete": complete, "truncated": len(rows) >= GRID_SCAN_CAP and not complete}

    def _op_select_row(self, grid: str, row: int, match: dict[str, Any] | None) -> dict[str, Any]:
        page = self._ensure()
        before = self._js("messages")
        target = self._js("row", grid, row, match)
        searched = None
        if target.get("error") == "not_found" and target.get("what") == "row" and match:
            found: dict[str, Any] = {}

            def hit(rows: list[dict[str, Any]], columns: list[dict[str, Any]]) -> bool:  # rendered now
                result = self._js("row", grid, 0, match)
                if not result.get("error") or result.get("error") == "links_only":
                    found.update(result)
                return bool(found)
            scan = self._scan_grid(grid, stop=hit)
            searched = len(scan["rows"])
            if found:
                target = found
            else:
                target = dict(target, searched_rows=searched, truncated=scan["truncated"])
        if target.get("error") == "links_only":
            raise D365WebError("refused", f"row {target['row']} of grid {target['grid']!r} has only hyperlink cells: "
                               "a click would open the record, not select the row; filter the list to it instead",
                               **{k: v for k, v in target.items() if k != "error"})
        if target.get("error"):
            raise D365WebError("not_found", f"no {'matching ' if match else ''}row in grid {grid or '(largest)'!r}"
                               + (f" (searched {searched} rows, scrolling)" if searched is not None else ""),
                               **{k: v for k, v in target.items() if k != "error"})
        page.click(target["x"], target["y"])
        # the pointer leaves the row for an inert spot of the form: resting on a linked record opens D365's
        # hover preview over the form (test box, 2026-10-26: the setup form's menu list); the page corner is
        # no good either, its Office 365 launcher shows a tooltip over the action pane
        rest = self._js("restPoint")
        if not rest.get("error"):
            page.move(rest["x"], rest["y"])
        return self._after_step(before, selected={"grid": target["grid"], "row": target["row"]})

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
        path = folder / time.strftime("%Y%m%d-%H%M%S.png")
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

    def lookup(self, name: str, pick: str | None = None, nth: int = 0) -> dict[str, Any]:
        check_operation("lookup", {"field": name, "pick": pick, "nth": nth})
        return self.call("web_lookup", lambda: self._op_lookup(name, nth, pick))

    def select_row(self, grid: str = "", row: int = 0, match: dict[str, Any] | None = None) -> dict[str, Any]:
        check_operation("select_row", {"grid": grid, "row": row, "match": match})
        return self.call("web_select_row", lambda: self._op_select_row(grid, row, match))

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
