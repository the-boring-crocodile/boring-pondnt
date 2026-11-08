(function (op, a, secretNamePattern) {
  const GRID = '[data-dyn-role=ReactList], [data-dyn-role=Grid]';
  const TAB_ROLES = new Set(['AppBarTab', 'TabPage', 'SectionPage', 'Tab', 'PivotItem']);
  const TAB = Array.from(TAB_ROLES, r => '[data-dyn-role=' + r + ']').join(',');
  const FIELD_ROLES = new Set(['Input', 'Real', 'Integer', 'Int64', 'Date', 'DateTime', 'Time', 'ComboBox',
    'CheckBox', 'SegmentedEntry', 'MultilineInput', 'Guid', 'RadioButton', 'QuickFilter', 'StaticText']);
  // Tile and CountTile: a workspace's tiles (a press opens their menu item, a count tile its filtered list)
  const BUTTON_ROLES = new Set(['CommandButton', 'MenuItemButton', 'Button', 'DropDialogButton', 'MenuButton',
    'ToggleButton', 'MenuItem', 'Tile', 'CountTile']);
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
  const secretName = new RegExp(secretNamePattern, 'i');
  const secretControl = (e) => {
    const i = inputOf(e);
    return !!(i && i.type === 'password') || secretName.test(cname(e));
  };
  const valueOf = (e) => {
    if (secretControl(e)) return '***';
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
  // D365's file upload control (DocumentUpload: the attachments' "Upload document" with its Browse button
  // over an input[type=file]; FileUpload on other forms): it opens the operating system's file picker
  const UPLOAD = '[data-dyn-role=DocumentUpload], [data-dyn-role=FileUpload]';
  const uploadOf = (e) => { const u = e.closest(UPLOAD); return u ? (cname(u) || drole(u)) : ''; };

  function scope() {
    const pops = Array.from(document.querySelectorAll('[data-dyn-role=Popup]')).filter(vis);
    if (pops.length) { const el = pops[pops.length - 1];
      return {el, kind: el.classList.contains('lookup-popup') ? 'lookup' : 'popup'}; }
    // a hover preview of a linked record (form *_AutoEnhancedPreview_*, or a preview form such as
    // InventTablePreview over a sales line's item) is not a dialog to work in
    const dialogs = Array.from(document.querySelectorAll('[role=dialog], [role=alertdialog]')).filter(vis)
      .filter(d => !/EnhancedPreview|Preview$|PreviewPart$/i.test(formName(d)));
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
        const c = cells.find(x => col.name && cellName(x) === col.name) || (cells.length === columns.length ? cells[k] : null);
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

  // Tree controls (data-dyn-role=Tree): li[role=treeitem] nodes with aria-level/-expanded/-selected and
  // D365's node id (data-dyn-id); the label is the node header's label span (aria-label may carry a
  // checkbox state such as "mixed"). Children of a collapsed node are not rendered.
  const TREE = '[data-dyn-role=Tree]';
  const nodeLabel = (li) => { const l = li.querySelector(':scope > .treeView-treeItem-header .treeView-treeItemLabel');
    return l ? text(l) : (li.getAttribute('aria-label') || ''); };
  function readTree(t, max) {
    const nodes = [];
    for (const li of t.querySelectorAll('li[role=treeitem]')) {
      if (!vis(li)) continue;
      if (nodes.length >= max) break;
      const n = {label: nodeLabel(li), level: Number(li.getAttribute('aria-level')) || 1};
      const ex = li.getAttribute('aria-expanded'); if (ex !== null) n.expanded = ex === 'true';
      if (li.getAttribute('aria-selected') === 'true') n.selected = true;
      nodes.push(n);
    }
    return {name: cname(t), nodes};
  }
  function treeNode(name, parent, label) {  // a node by its label among the children of `parent` (a node id; null: the roots)
    const sc = scope();
    const trees = queryScope(sc.el, TREE).filter(vis);
    const t = name ? trees.find(x => cname(x) === name) : trees[0];
    if (!t) return {error: 'not_found', what: 'tree', scope: sc.kind, trees: trees.map(cname)};
    if (!name && trees.length > 1) return {error: 'ambiguous', what: 'tree', trees: trees.map(cname)};
    const up = (li) => li.parentElement && li.parentElement.closest('li[role=treeitem]');
    const kids = Array.from(t.querySelectorAll('li[role=treeitem]')).filter(li => vis(li) &&
      (parent === null || parent === undefined ? !up(li) || !t.contains(up(li)) : !!up(li) && up(li).getAttribute('data-dyn-id') === String(parent)));
    const want = String(label).trim().toLowerCase();
    const hits = kids.filter(li => nodeLabel(li).trim().toLowerCase() === want);
    if (hits.length !== 1) return {error: hits.length ? 'ambiguous' : 'not_found', what: 'node', tree: cname(t),
      options: kids.map(nodeLabel).slice(0, 60)};
    const li = hits[0];
    const head = li.querySelector(':scope > .treeView-treeItem-header') || li;
    const lab = head.querySelector('.treeView-treeItemLabel') || head;
    lab.scrollIntoView({block: 'nearest', inline: 'nearest'});
    const r = lab.getBoundingClientRect(), btn = head.querySelector('.treeView-treeExpandCollapse');
    const b = btn && vis(btn) ? btn.getBoundingClientRect() : null, ex = li.getAttribute('aria-expanded');
    const out = {tree: cname(t), id: li.getAttribute('data-dyn-id') || '', label: nodeLabel(li),
      x: r.left + Math.min(r.width / 2, 30), y: r.top + r.height / 2, expanded: ex === null ? null : ex === 'true',
      selected: li.getAttribute('aria-selected') === 'true'};
    if (b) { out.bx = b.left + b.width / 2; out.by = b.top + b.height / 2; }
    return out;
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
    const uploads = queryScope(sc.el, UPLOAD).filter(vis).map(u => cname(u) || drole(u));
    if (uploads.length) out.uploads = uploads;
    const trees = queryScope(sc.el, TREE).filter(vis).map(t => readTree(t, 200));
    if (trees.length) out.trees = trees;
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
    let byName = true;
    if (!c.filter(visible).length) {
      byName = false;
      const want = name.trim().toLowerCase();
      c = all.filter(e => labelOf(e).trim().toLowerCase() === want);
    }
    const hidden = c.length;
    c = c.filter(e => visible(e) && inView(e) && !e.closest('[role=columnheader]'));
    if (kind === 'field') c = c.filter(e => !TAB_ROLES.has(drole(e)) && (inputOf(e) || isCheck(e)));
    c = c.filter(e => !c.some(o => o !== e && e.contains(o)));
    // web_state numbers a form's fields without the grid cells: a name both on the form and in grid
    // rows means the form's ([n] counts as in web_state); a name only in rows means the rows
    // (by control name only: a label such as Quantity found on a FastTab and in the active line keeps
    // the rows' active-line choice)
    if (byName) {
      const outside = c.filter(e => !e.closest(GRID));
      if (outside.length && outside.length < c.length) c = outside;
    }
    c.sort(byPos);
    return {list: c, hidden: hidden - c.length};
  }

  function locate(name, nth, kind, explicit) {
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
    // A field repeated in grid rows (a sales line's item): without an explicit occurrence ([n], also [0]), the active
    // row's cell -- where a user types after "Add line" -- not the first row's. The grid's viewmodel
    // knows the active row (its aria row number); the row classes do not (_highlighted is zebra striping).
    const activeRow = el => {
      const r = el.closest('[role=row]');
      if (!r) return false;
      let n = null;
      // live 10.0.49: the [role=grid] element carries the grid's name but no viewmodel; the grid
      // control's element above it has it (ReactList): the nearest ancestor that knows the active row
      for (let h = r.parentElement; h && n === null; h = h.parentElement) {
        try { const c = window.$dyn && $dyn.context(h); if (c && typeof c.getActiveRowAriaNumber === 'function') n = c.getActiveRowAriaNumber(); } catch (x) { n = null; }
        if (h.matches('[data-dyn-role=Form]')) break;
      }
      return n !== null && n !== undefined && String(n) === r.getAttribute('aria-rowindex');
    };
    // only for choosing the row to work in, never for model actions: the one row D365 marks selected
    const selectedRow = el => { const r = el.closest('[role=row]'); return !!r && r.getAttribute('aria-selected') === 'true'; };
    const editableCell = el => { const i = inputOf(el); return !!i && !readOnly(el); };
    const inRows = list.length > 1 && list.every(el => el.closest('[role=row]'));
    const selected = list.filter(selectedRow);
    const pick = kind === 'field' && !explicit && inRows ?
      (list.find(activeRow) || (selected.length === 1 ? selected[0] : null) || list.find(editableCell)) : null;
    const e = pick || list[nth], tab = kind !== 'field' && TAB_ROLES.has(drole(e));
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
    // a grid row's control: D365 applies a viewmodel command to the grid's current record, so the
    // model backend acts only in the active row (another row is clicked, which makes it active)
    if (e.closest('[role=row]') && e.closest('[role=grid]')) { out.in_row = true; out.active_row = activeRow(e); }
    // what a click at (x, y) would really hit: a teaching tip or other popup can lie over the control
    const hit = document.elementFromPoint(x, out.y);
    if (hit && !(tab ? target.contains(hit) : (e.contains(hit) || target.contains(hit) || hit.contains(target)))) {
      const tip = hit.closest('.qtip');
      out.covered = {by: ((hit.closest('[data-dyn-controlname]') && cname(hit.closest('[data-dyn-controlname]'))) ||
        (hit.className || '').toString().slice(0, 60) || hit.tagName), tip: tip ? tip.id : '',
        text: firstLine(tip || hit).slice(0, 80)};
    }
    const upload = uploadOf(e);
    if (upload) out.upload = upload;
    if (kind === 'field') {
      // a reference group by its own name: its parts are the fields to set (a click on the group can
      // land on its record hyperlink and open the referenced record)
      if (drole(e) === 'ReferenceGroup') out.parts = Array.from(new Set(Array.from(e.querySelectorAll('[data-dyn-controlname]'))
        .filter(k => vis(k) && inputOf(k) && !k.querySelector('[data-dyn-controlname]')).map(cname)));
      out.check = isCheck(e); out.value = valueOf(e); out.readonly = readOnly(e);
      if (secretControl(e)) out.password = true;
    }
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
      const hits = body.filter(r => keys.every(k => {
        const want = String(match[k]).trim().toLowerCase();
        const cells = cellsOf(r);
        if (k === '*') return cells.some(c => cellValue(c).trim().toLowerCase() === want);
        let c = cells.find(x => cellName(x) === k);
        if (!c) { const i = labels.findIndex(l => l[1] === k.toLowerCase()); if (i >= 0) c = cells.find(x => cellName(x) === labels[i][0]) || (cells.length === labels.length ? cells[i] : null); }  // by position only when the row has the header's cells (a marker cell shifts them)
        return !!c && cellValue(c).trim().toLowerCase() === want;
      }));
      if (hits.length > 1) return {error: 'ambiguous', what: 'row', grid: cname(g), matches: hits.length};
      row = hits[0] || null;
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
        if (hit && row.contains(hit) && !linkAt(hit)) return {x, y, grid: cname(g), row: body.indexOf(row), cell: '',
          key: row.getAttribute('aria-rowindex') || ''};
      }
      return {error: 'links_only', grid: cname(g), row: body.indexOf(row), cells: cells.map(cellName)};
    }
    const cell = plain || row;
    cell.scrollIntoView({block: 'nearest', inline: 'nearest'});
    const r = cell.getBoundingClientRect();
    return {x: r.left + Math.min(r.width / 2, 40), y: r.top + r.height / 2, grid: cname(g), row: body.indexOf(row),
      cell: cellName(cell), key: row.getAttribute('aria-rowindex') || ''};
  }

  // The grid's viewmodel (ReactList): the grid element itself or the nearest ancestor that knows the
  // active row (live 10.0.49: the [role=grid] element carries the name but no viewmodel).
  function gridModel(g) {
    for (let h = g; h; h = h.parentElement) {
      try { const c = window.$dyn && $dyn.context(h); if (c && typeof c.getActiveRowAriaNumber === 'function') return c; } catch (x) { /* none here */ }
      if (h.matches('[data-dyn-role=Form]')) break;
    }
    return null;
  }
  const peekOf = (v) => { try { return (window.$dyn && $dyn.isObservable && $dyn.isObservable(v)) ? $dyn.peek(v) : v; } catch (x) { return undefined; } };
  // The record a grid's viewmodel calls active (D365 applies commands and shows details for it): its
  // aria row number, tracking id and, when rendered, its cells. null: no viewmodel (a plain grid).
  function activeOf(g) {
    const c = gridModel(g);
    if (!c) return null;
    let aria = null;
    try { aria = c.getActiveRowAriaNumber(); } catch (x) { aria = null; }
    const out = {grid: cname(g)};
    if (aria !== null && aria !== undefined && aria !== -1) out.key = String(aria);
    const trk = peekOf(c.ActiveRowTrackingId);
    if (trk !== undefined && trk !== null && trk !== '') {
      out.tracking = String(trk);
      // a tracking id is per load (a refresh renumbers): the record id says whether the record moved
      try { const rec = c.getRecIdFromTrackingId(trk); if (rec !== undefined && rec !== null && rec !== '' && rec !== 0) out.recid = String(rec); } catch (x) { /* none */ }
    }
    if (out.key) {
      const read = readGrid(g, 1000);
      const row = read.rows.find(r => r.key === out.key);
      if (row) { out.row = row.row; out.values = row.values; }
    }
    return out;
  }
  // What a press or a row selection can move, read before and after it: the visible forms, the
  // dialog or popup in front, the active record (caption and every viewmodel grid's active row), the
  // pane (expanded action tab, open menu, expanded FastTabs and selected tabs) and a digest of the
  // scope's fields (values and read-only state: Edit, Calculate). Compared by the caller.
  function context() {
    const sc = scope();
    const forms = Array.from(new Set(Array.from(document.querySelectorAll('[data-dyn-form-name]')).filter(vis)
      .map(f => f.getAttribute('data-dyn-form-name'))));
    const visForms = Array.from(document.querySelectorAll('[data-dyn-role=Form]')).filter(vis);
    const main = visForms.length ? visForms.reduce((x, y) => area(y) > area(x) ? y : x) : document.body;
    const caption = Array.from(main.querySelectorAll('.titleField')).filter(vis).map(text)[0] || '';
    const rows = {};
    for (const root of new Set([main, sc.el])) {
      for (const g of queryScope(root, GRID).filter(vis)) {
        const a = activeOf(g);
        if (a && (a.recid || a.tracking || a.key)) rows[a.grid] = a.recid || a.tracking || a.key;
      }
    }
    for (const t of queryScope(sc.el, TREE).filter(vis)) {
      const sel = Array.from(t.querySelectorAll('li[role=treeitem][aria-selected=true]')).map(li => li.getAttribute('data-dyn-id'));
      rows['tree:' + cname(t)] = sel.join(',');
    }
    const pane = {action_tab: '', tabs: [], menu: openMenus().length > 0,
      tree_open: queryScope(sc.el, TREE + ' li[role=treeitem][aria-expanded=true]').length};
    for (const t of queryScope(main, '[data-dyn-role=AppBarTab]')) {
      const h = vis(t) && tabHeader(t);
      if (h && h.getAttribute('aria-expanded') === 'true') { pane.action_tab = cname(t); break; }
    }
    for (const t of queryScope(sc.el, '[data-dyn-role=TabPage], [data-dyn-role=SectionPage], [data-dyn-role=PivotItem], [data-dyn-role=Tab]')) {
      if (!controlVisible(t)) continue;
      const h = tabHeader(t);
      const ex = t.getAttribute('aria-expanded') || (h && h.getAttribute('aria-expanded'));
      const sel = h && h.getAttribute('aria-selected');
      if (ex === 'true' || sel === 'true') pane.tabs.push(cname(t));
    }
    const c = controls(sc.el, false);
    const flat = JSON.stringify([c.fields, Object.keys(c.readonly)]);
    let h = 5381;
    for (let i = 0; i < flat.length; i++) h = ((h * 33) ^ flat.charCodeAt(i)) >>> 0;
    return {forms, dialog: sc.kind === 'form' || sc.kind === 'page' ? '' : sc.kind + ':' + formName(sc.el),
      record: {caption, rows}, pane, fields: h.toString(16)};
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
    case 'locate': return locate(a[0], a[1] || 0, a[2] || 'any', !!a[3]);
    case 'grid': { const sc = scope(); const g = findGrid(sc.el, a[0]);
      return g ? readGrid(g, a[1] || 50) : {error: 'not_found', what: 'grid', scope: sc.kind,
        grids: Array.from(sc.el.querySelectorAll(GRID)).filter(vis).map(cname)}; }
    case 'row': return rowTarget(a[0] || '', a[1] || 0, a[2] || null);
    case 'context': return context();
    case 'treeNode': return treeNode(a[0] || '', a[1] === undefined ? null : a[1], a[2]);
    case 'active': { const sc = scope(); const g = findGrid(sc.el, a[0] || '');
      if (!g) return {error: 'not_found', what: 'grid', scope: sc.kind};
      return activeOf(g) || {grid: cname(g), unknown: true}; }
    case 'focusedAt': {  // does the keyboard focus sit in the control at (x, y)? typing goes there, nowhere else
      const hit = document.elementFromPoint(a[0], a[1]), f = document.activeElement;
      if (!hit || !f || f === document.body) return false;
      const ctl = hit.closest('[data-dyn-controlname]');
      return hit === f || hit.contains(f) || f.contains(hit) || (!!ctl && ctl.contains(f));
    }
    case 'gridScroll': { const sc = scope(); const g = findGrid(sc.el, a[0]);
      if (!g) return {error: 'not_found', what: 'grid', scope: sc.kind};
      const s = scrollerOf(g);
      const fdt = !s && g.querySelector('.fixedDataTableLayout_rowsContainer');
      if (fdt) {  // FixedDataTable grids hide their overflow and scroll on wheel events: send one to the
        // rows (a DOM event, not a mouse: no focus, no selection change). Progress: the last rendered row.
        const last = () => Math.max(0, ...Array.from(g.querySelectorAll('[role=row][aria-rowindex]')).map(r => +r.getAttribute('aria-rowindex') || 0));
        const before = last();
        fdt.dispatchEvent(new WheelEvent('wheel', {deltaY: a[1] === 'top' ? -1e7 : Math.max(24, Math.floor(fdt.clientHeight * 0.8)),
          deltaMode: 0, bubbles: true, cancelable: true}));
        return {scrollable: true, wheel: true, before, after: before};  // it redraws on its next frame: new rows tell
      }
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
    case 'listOpen': {
      if (a[0] === 'lookup') return scope().kind === 'lookup';
      const selector = a[0] === 'combo' ? '.comboBox-list' :
        a[0] === 'filter' ? '.quickFilter-dropdown' : '';
      return !!selector && Array.from(document.querySelectorAll(selector)).some(vis); }
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
          if (inp.type === 'password') c.password = true;  // masked by its kind, whatever its name
          if (inp.readOnly || inp.disabled) c.readonly = true;
          const l = labelOf(e); if (l) c.label = l;
        } else c.text = text(e);
        if (/^(CommandButton|Button|MenuItemButton)$/.test(role) && !enabled(e)) c.disabled = true;
        if (name === 'error') { const cs = getComputedStyle(e); c.bg = cs.backgroundColor; c.fg = cs.color; }
        controls.push(c);
      }
      return {caption: hdr ? text(hdr) : '', controls};
    }
  }
  return {error: 'unknown op ' + op};
})
