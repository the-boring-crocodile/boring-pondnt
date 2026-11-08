# boring-pondnt-d365web

Drive the D365 Finance and Operations web client through a signed-in Edge profile:
open a menu item, enter fields, press buttons, choose lookup values and inspect
the resulting form. This lets an agent check the visible workflow, including
dialogs and validation messages that a database query alone would miss.
Actions use the signed-in user's permissions and can change real business data.

The service controls the installed Microsoft Edge through the Chrome DevTools
Protocol, using Python's standard library. It has one browser and one active
step at a time. Use [D365 operations](D365_SERVICE.md) for build, sync and SQL,
and [d365whsapp](D365WHSAPP.md) for warehouse-app flows.

The MCP connection is **authenticated HTTP only; stdio is not supported**.
The recommended setup carries this backend through Ferry: d365web normally
listens on loopback port 18516, and Copilot reaches it through Ferry on 18517.
The browser's DevTools port (9333 in the example below) is a separate connection
between the service and Edge, not the MCP endpoint.

On a Windows development environment it runs under the person's account.
[pondNT setup](INSTALL.md) can expose it locally through Ferry; a remote Pond
can reach it through the [devbox tunnel](DEVBOX_TUNNEL.md).
Start with [Setup](#setup) and the [read-only first check](#first-check-open-and-inspect-a-list).
[Waiting and uncertain outcomes](#waiting-and-uncertain-outcomes) explains
what to do when a call returns before the UI settles.

## Setup

1. **A D365 sign-in and a service Edge profile.** The usual sign-in is the
   developer's D365 user. A dedicated D365 test user is optional, per box,
   where the tenant permits it; it needs the permissions for the planned checks.
   The service reuses the identity signed in to its profile. Choose the
   intended development/test environment and an existing company first;
   `usmf` below is a Contoso example.
2. **Sign the profile in once**, on the machine's desktop (an interactive session, not over SSH).
   The short way: double-click `browser-signin.cmd` in the release folder, as
   yourself. It asks for the service, suggests the AOS address from the box's
   `web.config`, asks for the company, writes `d365web.json` (step 3) when there
   is none, and opens Edge with the profile; an existing `d365web.json` is used
   and left alone. By hand:

   ```bat
   "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" ^
     --user-data-dir="%LOCALAPPDATA%\boring-pondnt\d365web\profiles\<profile>" ^
     --remote-debugging-port=9333 --remote-debugging-address=127.0.0.1 ^
     --no-first-run --no-default-browser-check "https://<aos host>/?cmp=usmf"
   ```

   Sign in, wait for the dashboard, close Edge. A profile is in use by one Edge at a time: close the
   server's browser first (`web_close`) when you sign in again.
3. **`d365web.json`** in
   `%LOCALAPPDATA%\boring-pondnt\services\d365web\d365web.json` for local setup
   (a tunnel launcher uses its separately configured service file):

   ```json
   {"url": "https://usnconeboxax1aos.cloud.onebox.dynamics.com", "company": "usmf",
    "profile": "d365web", "debug_port": 9333, "headless": true,
    "idle_minutes": 30, "step_timeout": 900}
   ```

   Optional `"backend": "model"` (the default) or `"input"`; see [Backends](#backends-model-and-input).

   | key | meaning |
   |---|---|
   | `url` | the AOS, https only; any path is dropped |
   | `company` | optional default for `web_open`; with neither, D365 opens the signed-in user's default company |
   | `profile` | folder under `%LOCALAPPDATA%\boring-pondnt\d365web\profiles`; use the same name in the sign-in command. `profiles_dir`, when set, is the complete profile directory, not its parent |
   | `debug_port` | fixed loopback port: an Edge left running from an earlier server start is attached to again instead of failing on a profile in use. 0 = random |
   | `headless` | `true` (default) launches without a visible window; `false` shows it for desktop diagnosis. Since 0.2.8 a visible Edge also keeps rendering when its window is covered or nobody views the session (Windows occlusion detection and background throttling are switched off for it) |
   | `idle_minutes` | closes the service browser after this much idle time, including one reattached after a service restart |
   | `step_timeout` | how long one step may wait for the client to become idle (default 600 s; forms and postings can take minutes; calls answer `running` meanwhile and `web_wait` collects) |

4. `boring-pondnt-d365web serve --config <service.json> --d365web <d365web.json>`, or
   select `d365web` through [pondNT setup](INSTALL.md) (port 18516; refused while
   `d365web.json` is missing). Keep your complete intended service list and use
   `-Gateway all` for the recommended routing: core and Knowledge direct, the
   other selected services through Ferry.

### Optional dedicated D365 test user

Keep the same Windows account that runs the backend. Close its browser with
`web_close` before changing profile settings. In the complete `d365web.json`,
choose a separate `profile`, such as `d365web-test`, and an unused
`debug_port`, such as 9335. If `profiles_dir` is set, it overrides
`profile`; choose a separate complete directory there instead.

Run `browser-signin.cmd d365web` on that account's desktop. The helper reads
the existing configuration and leaves it unchanged. Sign in with the chosen
test user's D365 account, using the tenant's account-selection flow; wait
for the intended environment/company and close Edge. Restart the backend
through setup with the complete installed service selection to load changed
settings. Check `web_state` after opening the intended menu item.

This changes the browser's D365 identity, not the Windows process identity.
The developer sign-in remains a valid choice. Tenant policy, user licensing
and D365 permissions stay with the environment's administrator.

The service can attach to an Edge already listening on its configured debugging
port. Reserve that port for this profile; an occupied port alone does not establish
which browser owns it. Keep unrelated browsing in another profile. When changing
`headless`, finish and collect the current operation, close the service browser,
then restart the service with the changed configuration. Attaching to an existing
browser does not change how that browser was launched.
An attached browser reports `browser_mode_mismatch` when its command line differs
from the configured headless mode, or `browser_mode_unknown` when Edge cannot
supply that evidence.

## Tools

Page actions return the observed state afterwards; long operations return
`running` first and need `web_wait`. `web_close` returns a closure receipt.
Discovery and named actions use the active form, dialog or lookup, including the
visible detached action pane belonging to an expanded tab in that scope. The
pane can stay unpinned; controls in collapsed panes or another form are excluded.
Pressing a tab or FastTab targets its own header, including when its content
contains links. A missing or ambiguous visible header is refused with
`reason: tab_header_unavailable`; discover the actual control before continuing.
A FastTab or section press toggles it, and D365 remembers expanded/collapsed per user across
sessions: check whether the field you need is in the state before pressing its tab.
A `pressed` response records the dispatched press (a model command or a click; see Backends); it is
not proof that anything happened. `web_press`, `web_select_row` and `web_tree` therefore compare the
page before and after and say what moved:

- `changed`: the parts that moved: `form` (the visible forms), `dialog` (the one in front),
  `record` (the caption, a grid's active record by its record id, a tree's selected node), `pane`
  (the expanded action tab, an open menu, expanded FastTabs and selected tabs, open tree nodes),
  `fields` (values or read-only state in the scope), `messages`, `address`, `browser_dialog`.
- `unchanged: true` with a `note` when none did: D365 did nothing visible (a swallowed click, a
  command with no effect here, or work that shows nowhere on this page). Inspect with `web_state`
  before pressing again; a business action is never retried for you.

Neither is said while D365 is still busy (`idle: false`) or when the page could not be read.

| tool | does |
|---|---|
| `web_open(menu_item, company?, kind?)` | navigate to `/?cmp=<company>&mi=<menu item>` (`kind`: action, output); refused while the state reports `unsaved` |
| `web_state(full?, max_rows?)` | the state now; `full` adds labels and other controls |
| `web_set(values)` | set fields in order (see Backends: through the control's viewmodel, else click, select all, type, Tab); checkboxes take yes/no. Use `[{"field": "Name", "value": "text"}, ...]`, the advertised MCP schema. Legacy pairs and object maps are also accepted, but maps do not reliably preserve typing order through clients |
| `web_press(name, nth?)` | press a button, action tab, FastTab or link |
| `web_lookup(field, pick?, nth?)` | open a field's lookup; with `pick`, choose the row holding that value |
| `web_filter(value, column?)` | the list page's quick filter: `value` in `column` (header label or column name; default the first column); `""` clears |
| `web_select_row(grid?, row?, match?)` | select a grid row by position (rendered rows) or by `{column: value}`, scrolling the grid to find it; answers the `active` record |
| `web_tree(path, tree?)` | select a tree node by its labels from the root (`["Bank", "Cheques"]`), expanding each parent |
| `web_grid(grid?, max_rows?, start_row?, all_rows?)` | columns and rows of a grid: the rendered rows, or later rows / all rows by scrolling (see Grids) |
| `web_key(keys)` | chords to the focused element: `Escape`, `Alt+N`, `Alt+F9`, `Tab Tab Enter` |
| `web_click(x, y)`, `web_type(text)` | fallbacks by coordinates / into the focused element |
| `web_run_scenario(steps, variables?, stop_on_mismatch?, full_final?)` | many steps in one call, each with expectations; see below |
| `web_wait(seconds?)` | the result of a step that answered `running`, else the state once idle |
| `web_screenshot()` | a PNG next to the server's state (path on the server machine) |
| `web_close()` | close the service browser, including one reattached on its debugging port |

The state:

```json
{"scope": "form", "form": "PurchTable", "caption": "00000271 : 1001 - Acme Office Supplies",
 "messages": [{"severity": "warning", "text": "Field 'Site' must be filled in."}],
 "new_messages": [...], "fields": {"PurchLine_ItemId": "A0001"}, "readonly": {"PurchStatus": "Open order"},
 "buttons": ["SystemDefinedSaveButton", "buttonConfirm"], "disabled": ["buttonRequestChange"],
 "action_tabs": ["PurchOrder (Purchase order)", "Purchase"], "tabs": ["LineViewLines (Purchase order lines)"],
 "grids": [{"name": "LineSpec", "columns": [{"name": "PurchLine_ItemId", "label": "Item number"}],
            "rows": [{"row": 0, "values": ["A0001"], "selected": true}], "visible_rows": 1}],
 "url": "/?cmp=usmf&mi=PurchTableListPage", "company": "usmf"}
```

- **Names** are the client's `data-dyn-controlname` values; a label works too ("Save", "Vendor
  account"). A miss answers `not_found` with `similar` names.
- **Scope**: only the top-most container counts: an open lookup, else the last dialog (including
  message boxes), else the largest visible form. Hidden forms stay in the page and are ignored.
- **`nth` / `Name[n]`** count visible matches top to bottom: in a grid, `PurchLine_PurchQtyGrid[1]` is
  the quantity in the second visible row. Without `[n]`, a field repeated in grid rows means the
  grid's active row (where a user types after "Add line"), else the first editable occurrence;
  `[0]` is the first row. `web_lookup`'s `nth` works the same way (omitted: the active row).
- **`unsaved`** lists the visible forms whose records hold unsaved changes (D365's own
  `AreDataSourcesDirty`). `web_open` is refused while it is set: navigating away drops the changes
  without D365 asking. Save (`SystemDefinedSaveButton`) or leave the form with
  `SystemDefinedCloseButton`, which saves valid changes. A new record in a create dialog counts, and
  so does a parameter form right after it opens (D365 initialises its record).
- **A field that opens a dialog** (D365 asks before applying a customer group's dimensions) stops
  `web_set` there (`stopped_at`); answer the dialog, then set the remaining fields.
- **Message boxes** ("Delete record?") are dialogs too: the question is the `caption`, the answer
  `web_press Yes` / `No`.
- **Hidden buttons** live in collapsed action tabs: press the tab (`Purchase`), then the button.
- **Toggles and enum combo boxes**: `web_set` clicks a toggle (`yes`/`no`) and
  picks a combo-box value from its rendered popup. Use the label in the user's
  language. A refused option includes the discovered `options`; an empty list
  means none were discovered, not proof that D365 offers no choices. Compare
  the visible popup before treating it as an invalid value.
- **Required and dropped values**: `missing` lists required fields that are still empty; an
  `applied` line with `differs: true` means D365 did not keep the value (it reformats 125 to 125.00
  and 618160 to 618160-001--- without `differs`, but silently clears e.g. an account number that does
  not fit the number sequence).
- **Dependent fields**: a field still read-only is retried for 3 s (D365 enables the warehouse a
  moment after the site), then refused.
- **Messages** appear once: `new_messages` holds what the step brought, `messages` the older ones
  still shown (up to 2000 characters each).
- D365 reports rejected values as warnings, so `web_set` stops at the first new error or
  warning (`stopped_at`) and `applied` shows what each field holds afterwards.
- **Menu buttons** ("Save and open"): pressing one shows its items as `menu`; `web_press` looks in an
  open menu first, by name or label (`web_press "Sales order"`).
- **Labels**: `labels` maps names to labels only where the name does not already say it
  (`Proforma` → "Posting", `buttonUpdateConfirm` → "Confirm now"); `full=true` shows every label.
- **Hyperlinks** in grid cells (`input.dyn-hyperlink`) open the record only when their text is hit;
  `web_press` clicks near the start of the text, `web_select_row` avoids link cells.
- **Active record**: `web_select_row` answers `selected` (the clicked row) and `active`: the record
  the grid's viewmodel calls active (`row`, `key`, cell `values`, `caption`), the one the detail
  fields and commands work on. `active_differs: true` means it is not the clicked row (the click was
  swallowed, or D365 kept the old row): check before acting on the details.
- **Trees** (`trees` in the state): the rendered nodes with `label`, `level`, `expanded`,
  `selected`; children of a collapsed node appear once it is expanded. `web_tree` matches labels
  exactly (case and outer spaces aside); a missing or repeated label is refused with the labels on
  offer (`options`). Node checkboxes are never touched.
- **Workspaces**: tiles and count tiles are buttons (`web_press AllCustomers` opens the list).
  Workspace card lists are not grids and are not read: open their list page instead.
- **Drop dialogs** ("Get a link", "Add to workspace"): a press opens them as a dialog scope;
  `web_key Escape` closes one.
- **Wizards** are forms or dialogs with `Next`, `Back` and `Cancel` buttons; their pages are tabs.
- **Reference groups** (a worker shown by name): set the group's field as `web_state` lists it
  (`Worker_DirPerson_FK_Name`); D365 resolves the typed text. The group's own name is refused with
  its fields (`parts`): a click on the group can open the referenced record.
- **File uploads** (`uploads` in the state) are not supported: pressing or setting anything in an
  upload control is refused (`upload`), as Browse opens the operating system's file picker. Cancel
  the dialog, or upload the file by hand.

## Backends: model and input

`backend` (in `d365web.json`) chooses how `web_set`, `web_press`, `web_lookup`, `web_filter` and the
grid reads act:

- **`model`** (default): through D365's own client viewmodels -- the objects behind its controls --
  with the commands a control itself sends to the server (`SetValue`, `SetSelectedItem`,
  `toggleValue`, `Clicked`, `ExpandedChanged`, a lookup's `SelectionCompleted`, a grid's
  `LoadNextPage`, the quick filter's `ApplyFilter`). No mouse, no keys, no popups opened to pick
  from: D365 validates as for a user, and its readback is reported as usual.
- **`input`**: synthetic mouse and keyboard input, as before 0.2.10.

The model path is per step: where a control has no usable viewmodel the input path runs for that
step, and `applied`/the answer say `via: model` or `via: input`. Known input steps in model mode:
grid cells (they have no viewmodel), dates in the user's display format (`11/20/2026` is typed;
ISO `2026-11-20` goes through the model), times, reference-group fields (`SetValue` on them is not
resolved: D365 restores the old text), menu buttons (their list opens on a click), tree nodes (the
tree's commands take node objects the page does not hand out), tab headers (PivotItem), record
hyperlinks in grids, a lookup pick that is a shown text only or held by more than one row, a control
in a grid row other than the active one (D365 applies a viewmodel command to the grid's current
record; the input path clicks that row, which makes it active), a covered control (the model finds
the control at its position), a quick filter that is not the only one of the scope's form.

A combo value that is not an option is refused with `options` and nothing is typed; a combo already
holding the value is not set again. A lookup pick matches the rows' text values only (not numbers or
flags in hidden columns); missing from all of the lookup's rows it is refused and the lookup closed
without changing the field. After a model pick the answer has the field's `now` and `differs` when
the field holds none of the chosen row's values. A grid read or lookup stops asking for more rows
after three pages that bring none.

Never guessed: a lookup pick or a `web_select_row` match that more than one row holds is refused
with the count (give a value only one row holds); text is typed only into the field that took the
keyboard focus after its click; `web_open` is refused when D365 does not answer whether the page
holds unsaved changes. The idle close (`idle_minutes`) waits while a form holds unsaved changes.
A browser `confirm()` or `prompt()` is cancelled (`browser_dialogs_dismissed`): it may approve
something nobody asked for; an alert or a leave-page question is accepted (`browser_dialogs_accepted`).
Ambiguity is judged among the rows a search has seen: `web_select_row` stops at the first window
that holds a match, so a duplicate further down is not seen; filter the list when values repeat.
A name both on the form and in a grid's rows means the form's field (`[n]` counts as in
`web_state`); a name only in grid rows means the rows. An input-backend grid read that had to
select the last row and page down to load more says `selection_moved`. d365web attaches only to an
Edge on its own profile (another profile's Edge on the debugging port is left alone), and the idle
close waits while d365whsapp's form tabs hold logged-in workers.

## Waiting and uncertain outcomes

D365 keeps background connections open, so network silence is not a useful
"fully loaded" test. This client waits for `$dyn.clientBusy()` to remain false
for 0.4 seconds, no processing-progress dialog, and no `aria-busy` on the main
area. That establishes a quiet UI, not that a click opened the intended dialog
or a transaction was saved.

| Observation | What to do |
|---|---|
| `status: running` | Call `web_wait` until it returns the original result or error. Other steps, including state reads, are refused as `busy` while that result remains uncollected. |
| `idle: false` | The configured settle timeout expired while D365 still appeared busy. Use `web_wait` to observe it; do not repeat the action. |
| `unchanged: true`, or `pressed` without the expected transition | Inspect the returned state, then the current state once no result is pending. Dispatch is not confirmation that D365 handled it; nothing is retried for you. |
| Timeout or connection error after dispatch | The action may have run. Check application state and any persisted effect before deciding to resubmit. |
| A visible browser behaves differently | Before 0.2.8, a visible Edge in a covered, locked or unviewed session stopped rendering, and the first click or key after `web_open` could be lost. 0.2.8 launches it with occlusion detection and background throttling off. If it still differs, compare a fresh headless launch. A longer timeout cannot establish that input reached the intended control. |

`step_timeout` bounds a wait for the client to settle; it is not a business
transaction deadline and does not cancel or roll back processing. Slow storage,
a cold AOS and browser visibility can affect a run differently. Record the
form, company, language, browser mode and observed state when investigating;
do not turn a successful retry into proof of a timing cause.

## The in-form WHS app simulator

d365web has no warehouse-app tools. The in-form simulator
(`action:WHSWorkExecute`) belongs to [d365whsapp's form mode](D365WHSAPP.md#mode-form-the-in-form-simulator):
the same `whs_*` tools as its protocol mode, using a separate Edge profile and
this service's browser library.

## Grids: rendered rows, paging, limits

D365 grids are virtualized: only the rows near the visible part of the list are in the page,
and the next block of records loads when the list is scrolled to its end. A row the person
would have to scroll to is not there for a reader of the page either.

- `web_grid` without `start_row`/`all_rows` and `web_state` return the rendered rows only
  (`web_state`: 20 by default, `max_rows` up to 200; `web_grid`: 50 by default, up to 200).
- `web_grid(start_row=N, max_rows=M)` scrolls the list from the top like a person, collecting
  rows until N+M are known; `more` says whether rows follow. `all_rows=true` scrolls to the end.
  Grids that hide their overflow (FixedDataTable) are scrolled with a wheel event on their rows;
  this moves no selection.
- With the model backend, a list with more records than loaded asks the grid for its remaining
  blocks (`LoadNextPage`) and reads again; it never clicks a row or presses PageDown. Without a
  grid viewmodel it answers `more` instead. The input backend selects the last row and pages down.
- Rows are told apart by D365's row index (`aria-rowindex`, returned as `key`), else by their
  values. A scan stops at 1000 rows (`truncated: true`): filter the grid instead.
- `web_select_row(match=...)` and a scenario's `expect.grid.contains` search the same way when
  the row is not rendered; a miss says how many rows were searched.
- The list is left scrolled where the scan ended.

Filtering on the document first (`web_filter`) is still the consultant's way to find one
record in a long list, and it keeps the check fast as the list grows.

## Scenarios

`web_run_scenario` runs many steps in one call (as one pending step: `running` → `web_wait`).
This example filters an existing test order and checks its customer; replace the
company, order, customer and localized column labels with your fixture values:

```json
{"variables": {"so": "000861"}, "steps": [
  {"op": "open", "menu_item": "SalesTableListPage", "company": "usmf"},
  {"op": "filter", "value": "${so}", "column": "Sales order",
   "expect": {"grid": {"rows": 1, "contains": {"Customer account": "US-902"}}},
   "save": {"caption": "caption"}}]}
```

- `op`: open, state, set, press, lookup, filter, select_row, key, tree, with the arguments of those tools.
- `expect`: `scope`, `form`, `caption` (substring), `fields` {name or label: value, D365 formatting
  allowed}, `editable` [fields], `message` (substring of a new message), `no_errors` (no new error or
  warning), `enabled` / `disabled` [buttons], `grid` {`name`, `rows`, `contains` {column: value}}.
- `save` accepts `"caption"`, `"field:NAME"`, `"message"`, `"grid:COLUMN"`, or
  {`from`, `regex`, `row`, `grid`}. A grid column matches its technical name or label,
  searching grids in page order and using the first grid containing that column.
  To choose a grid, use `{"from": "grid:WorkUser", "grid": "WorkUsers"}`.
  Use `${name}` in later steps. The full list is checked before submission: required arguments,
  types, nested expectations and save specifications (including regex syntax). Unknown variables
  and unknown keys are refused before anything runs. Variables saved by an earlier page are
  checked when available; invalid or missing saved data stops the dependent step with the prior
  transcript retained. Numeric row/count strings are accepted; indices/counts must be nonnegative
  integers (max_rows at least 1). Save specs are static and are not variable-substituted.
- The answer: `ok`, one transcript line per step (`at`, `caption`, `messages`, `differs`, `unchanged`,
  `changed`, `active_differs`, `saved`,
  `mismatch` or `error`), `variables`, and `final` (scope, form, caption, messages; `full_final` for the page).

An example scenario across both services: `tests/scenarios/example-purchase-receipt.json`.

## First check: open and inspect a list

In a Contoso test environment where USMF exists, open a standard list and inspect
it without creating a record:

```json
{"menu_item": "PurchTableListPage", "company": "usmf"}
```

Pass those arguments to `web_open`; collect with `web_wait` if it returns
`running`. Then call `web_state(full=true)`. Check the company, form/caption
and visible controls against the browser. An empty purchase-order list is a
valid result. Use your actual company elsewhere; do not copy a demo company
into a customer configuration.

For an action test, choose a disposable fixture and an explicit expected
transition. A scenario expectation checks the UI it can observe; verify a
persisted business effect separately when that is the test's goal.

### From nothing to the first check, in one go

The steps above, joined for a box where pondNT is already installed with the
recommended layout (core and Knowledge direct, the rest behind the gateway),
as you would do them on the box's desktop:

1. **Sign in a dedicated profile and write the configuration:** double-click
   `browser-signin.cmd` in the release folder, as yourself (not elevated).
   Answer `d365web`, take the suggested AOS address or type yours, and the
   company. It writes
   `%LOCALAPPDATA%\boring-pondnt\services\d365web\d365web.json` (setup refuses
   d365web while it is missing) and opens Edge with the profile. Sign in, wait
   for the dashboard, close Edge.
2. **Check what it wrote** (Setup 3 explains each key):

   ```powershell
   Get-Content "$env:LOCALAPPDATA\boring-pondnt\services\d365web\d365web.json"
   ```

3. **Add it with everything you already have** (an elevated PowerShell in
   the release folder; list every installed service, see
   [Update, and back](INSTALL.md#10-update-and-back), step 2):

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\setup.ps1 -WhatIf -SkipIndex -Gateway all -Services 'sourcecache,knowledge,canvas,core,d365,d365web' -Index J:\pondnt\d365.db
   powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Yes -SkipIndex -Gateway all -Services 'sourcecache,knowledge,canvas,core,d365,d365web' -Index J:\pondnt\d365.db
   ```

   These commands cover the five default services plus d365web. Retain
   any installed d365whsapp or d365data in both service lists too; prepare
   their configuration before selecting them. The preview should show the
   intended full selection and routing. Existing keys are kept when valid
   with at least 30 days left. Client publication can change entries or
   credentials when needed; inspect setup's result and refresh the client
   if its managed configuration changed.
4. **Find it through the gateway.** In Visual Studio (Copilot, Agent mode),
   ask for boring-gateway's `service_list`: `d365web` is listed and answers.
   `service_describe` with `{"service": "d365web"}` shows its tools and this
   page's guidance.
5. **The read-only first check**, through the gateway:

   ```json
   service_call  {"service": "d365web", "tool": "web_open", "arguments": {"menu_item": "PurchTableListPage", "company": "usmf"}}
   service_read  {"service": "d365web", "tool": "web_wait", "arguments": {}}
   service_read  {"service": "d365web", "tool": "web_state", "arguments": {"full": true}}
   ```

   `web_wait` is needed only if `web_open` answered `running`. The state
   shows the purchase order list (form `PurchTable`, "All purchase orders")
   with its filter and buttons; nothing was created. `service_call` with
   `web_close` ends it. Steps 4 and 5 were tried once like this on our test
   box, through its real gateway (pondNT 0.2.8, application 10.0.49), where
   d365web was already set up. That live check establishes discovery and
   inspection, not a fresh-install run of this whole recipe; steps 1 to 3
   follow the shipped sign-in helper and setup implementation.

If step 5 answers `signin_required`, the profile's sign-in expired or
belongs to another profile: run `browser-signin.cmd` again (it takes the
profile from `d365web.json`).

### Earlier workflow evidence

On a development box, D365WEB created and confirmed purchase and sales
orders, created a customer, posted a general journal and a free-text invoice,
and read an on-hand inquiry. Those are observations of that environment and
that release, not a first-run recipe or acceptance of every form, extension,
browser mode or later release.

## Limits

- One browser, one tab, one step at a time; parallel tests need separate servers and profiles.
- Cold-start time depends on the environment. Observe the running result and
  busy indicators instead of relying on a fixed startup delay.
- When the profile's sign-in expires, steps answer `signin_required`: sign in again (Setup 2).
- Steps change real data in the connected D365.
- Not supported: file uploads (refused), workspace card lists (not read), tree node checkboxes.
  Grid cells have no viewmodel of their own: they are set through the input path.
