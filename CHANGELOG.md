# Changelog

Notable changes per release, newest first ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
[SemVer](https://semver.org/); tags `vX.Y.Z`, candidates `vX.Y.Z-rc.N`).

## 0.2.10 — 2026-11-08

Built on Pond Common 0.7.13 and bundles MCP Ferry 0.1.5, Canvas 0.2.20,
Knowledge 0.2.16, D365 Sourcecache 0.1.19 and Fishmarket 0.1.15.

Added:

- **d365data**, a new opt-in server for a D365 environment's data, reached through Ferry like the
  browser and warehouse servers (port 18518; [D365DATA.md](docs/D365DATA.md)). OData:
  `odata_entities`, `odata_query` (bounded pages, continuation, `count`, company handling,
  `cross_company` across all companies the mapped user sees), `odata_write` (create, update,
  delete; keys built by type; `etag` makes update and delete conditional, `conflict`) and
  `odata_action`. Data management packages: `dmf_export`, `dmf_import` (can continue in the
  background, polled by submission id) and `dmf_status` (errors for every state, optional
  download). It needs its own Entra app and client secret (DPAPI) and only the environment's URL,
  so it works against a dev box or a sandbox. Rows, written records and action values are returned
  unmasked; diagnostic text masks storage URLs and credentials. Results are cut at a stated size
  (`max_chars`, up to 262,144) and say how to continue. The devbox tunnel forwards it on 18528.
- **`d365_logs`** in the D365 server reads batch infologs, process automation messages, Windows
  events and IIS access logs: severity totals, grouping across changing IDs and OData keys, `job`
  and `status` filters, `until` for an older window and lossless paging (`until` plus
  `until_skip`). The database log is read through d365data's `DatabaseLogs` entity.
- d365web **backends**: `model` (the default) acts through D365's own client viewmodels and the
  commands a control sends to the server; `input` keeps the synthetic mouse and keyboard path. A
  control without a usable viewmodel falls back to input for that step, and the answer reports
  `via`. See [D365WEB.md](docs/D365WEB.md#backends-model-and-input).
- d365web says what moved: `web_press`, `web_select_row` and `web_tree` answer `changed` (form,
  dialog, record, pane, fields, messages, address, browser dialog) or `unchanged: true` with a
  note to inspect before pressing again; nothing is retried. `web_select_row` reports `active`
  (and `active_differs` when D365's active record is not the clicked row).
- d365web: tree controls (`trees` in the state, `web_tree(path, tree?)`, scenario op `tree`)
  select a node by its label path; workspace tiles and count tiles are listed and pressed as
  buttons; file upload controls are listed as `uploads`.
- d365web: `unsaved` in every state lists the visible forms holding unsaved changes; `web_open`
  and the idle close refuse to navigate away while it is set or unknown.
- d365whsapp: `whs_logoff` answers `retry_later` (nothing pressed, session kept) while the logoff
  menu names are still being looked up, as on a cold AOS after a restart; `whs_login` can answer
  `retry_later` too. Logoff finds environment-specific menu buttons through the standard LogOff
  activity in both modes, with optional configured names and an English fallback.

Changed:

- The core's call log is now `calls-core.jsonl` (was `calls.jsonl`, the same name as the bundled
  Knowledge's), so a support bundle can collect several state folders without one overwriting
  another. An existing `calls.jsonl` is left where it is.
- Reproducible release zips: two builds of one commit with the same inputs give the same zip
  (uv's per-install cache file is dropped, every entry carries the commit's time and one file mode,
  MANIFEST.json records `commit_time` instead of the build time).
- Candidate builds carry the rc number in the package version, manifest and zip name
  ([INSTALL](docs/INSTALL.md)); the published build does not.
- File tools refuse special paths: network shares (UNC), device paths, alternate data streams and device names (`CON`, `NUL`, `LPT9 .txt`, ...).
- d365web: a reference group set by its own name is refused with its fields (`parts`); pressing or
  setting a file upload control is refused (`upload`) instead of opening the file picker; a combo
  value that is not an option is refused with its `options`; a lookup or row pick held by more than
  one row is refused, not guessed; a browser `confirm()` or `prompt()` is cancelled
  (`browser_dialogs_dismissed`).
- d365web attaches only to an Edge on its own profile and refuses a foreign Edge on the configured
  debugging port.
- d365whsapp form mode: `whs_login` for a worker whose tab still holds a session resumes it, as app
  mode does. Another device taking the worker ends the local session as `session_taken`; a second
  call for a worker while one runs is refused as `busy`; an error after Login was pressed answers
  `login_outcome_unknown` (do not retry the password).
- `d365_job`: reading and waiting need the reader role, stopping needs writer.
- d365whsapp uses the shared Entra client; malformed token replies report `token_failed`.
- The shared HTTP transport no longer follows redirects; WHS `aos_url` must be https.
- Tool descriptions and schemas explain accepted values, limits and follow-up calls.
- Setup lists new handbook pages separately from differences, gives every gateway backend a 900 s
  read timeout, refuses `%` in paths, and refuses cleanly on a missing `keys.json` or a bad
  `ferry.json` without leaving a staging folder behind.

Fixed:

- d365whsapp form mode logs in again (since 0.2.5 it stopped before pressing Login, because D365's
  password field never reads back what was typed). A password set through the model is confirmed
  by D365's own empty-password flag.
- WHS logoff: leaves an information page through its OK or Close button only, never a page that
  asks something; an unanswered logoff lookup is replaced after 150 s; one walk-out limit in both
  modes.
- d365web model backend: menu buttons open by a click; reference-group fields are typed for D365
  to resolve; lookup picks match the rows' text values only and read the field back (`now`,
  `differs`); a control in a non-active grid row goes through the input path; an explicit `[0]`
  means the first row; date and password readbacks read the written control.
- d365web grids: FixedDataTable grids are scrolled to the end (before, `all_rows` could stop at
  part of the list); paging stops after three empty pages; a field repeated in grid rows targets
  the active row.
- d365web input path: types only into the field that took the focus, never a line break into a
  one-line field; value checks no longer accept a longer value or read `1,5` as 15; punctuation
  keys send their own virtual keys; a stray Escape no longer navigates back; `web_set` stops at a
  field that opens a dialog (`stopped_at`); hover previews no longer count as the dialog.
- d365web masks secret fields in page and applied results and in the call log.
- d365data: every request is bounded, the token included; a 5xx after a write says it may have
  completed; a GET answered 401 is repeated once with a new token.
- `d365_logs`: Windows events are queried by a UTC window (daylight saving no longer shifts it);
  a large IIS log's tail uses the file's `#Fields`.
- `d365_sql`: the per-batch timeout takes effect; a failed rollback no longer hides the error.
- Redaction masks JWTs, bearer headers, access keys and `api_key`/`credential` fields, and leaves
  ordinary prose alone.
- Jobs: a job whose output passes 1 GiB on disk is stopped; ended jobs past retention are forgotten
  while the server runs.
- Shared HTTP startup waits up to 20 s for an old listener to release its port during a restart.
- The gateway starts when a `guide_file` cannot be read (Ferry 0.1.5).

Security:

- d365data uses a dedicated Entra app and D365 user: System administrator on a dev box for
  regression, a limited role on shared boxes. [D365DATA.md](docs/D365DATA.md) records the tested
  permissions and the package storage it accepts.
- The file tools' refusal of network shares (above) stops a read from sending the account's NTLM
  hash to another host.

## 0.2.9 — 2026-11-01

Same Pond Common (0.7.11) and bundled services as 0.2.8.

Security:

- **setup.ps1 locks down its own release folder.** The folder's Python runs as SYSTEM and
  elevated, but a folder unpacked under `C:\` lets every user add files to it, enough to run
  code as SYSTEM. setup now takes exactly that folder off inheritance, asking first unless
  `-Yes` (administrators and SYSTEM change it, Users read and run it), checks every file below
  it, and starts no service while anything else may write there. Existing installs get it at their next setup
  run. INSTALL.md shows the same as one `icacls` line.

Added:

- **`browser-signin.cmd`** in the release folder signs in the Edge profile that d365web, or
  d365whsapp in form mode, drives. Double-click it as yourself: it suggests the AOS address
  from the box's `web.config` (`Infrastructure.HostUrl`), asks for the company, writes the
  service's configuration file when there is none (an existing one is used and never
  overwritten), refuses while the service's browser holds the profile's port, and opens Edge.
  App mode (client secret) is left alone.

Docs:

- New: [what has been tested where](docs/EVIDENCE.md), [who can do what on the box](docs/THREAT_MODEL.md)
  (including the two loopback doors without a key: Canvas's page and d365web's debugging port),
  [other clients on the same box](docs/CLIENTS.md) (untested recipes), [update, and back](docs/INSTALL.md#10-update-and-back), and
  [the browser from nothing to the first check](docs/D365WEB.md#from-nothing-to-the-first-check-in-one-go).
- INSTALL lists every server, checks long paths, and explains the folder permissions; the call log's
  description matches the code (no argument text at all; rotation is a size rule, not retention).

Fixed:

- The bundled wheels are reproducible (a fixed SOURCE_DATE_EPOCH, like Pond Common's build).
- The package no longer describes itself as a stdio server; every server speaks HTTP.
- `setup.ps1 -?` describes `-Uninstall` completely (it also stops and removes pondNT's own
  scheduled tasks); the CI guest script no longer claims the installer enables long paths.

## 0.2.8 — 2026-10-30

The first stable release after 0.2.0rc23; everything since. Built on Pond Common 0.7.11 and
bundles Canvas 0.2.18, Knowledge 0.2.15, D365 Sourcecache 0.1.18, Fishmarket 0.1.14 and MCP
Ferry 0.1.3.

New servers:

- `boring-pondnt-d365web` ([docs/D365WEB.md](docs/D365WEB.md)) drives the D365 web client in a
  signed-in Edge over the DevTools protocol, standard library only. Tools `web_open`,
  `web_state`, `web_set`, `web_press`, `web_lookup`, `web_select_row`, `web_grid`, `web_filter`,
  `web_key`, `web_run_scenario`, `web_wait`, plus coordinate fallbacks. Every step returns a
  compact state (scope, fields, buttons, grids, messages). Grids are scanned the way a person
  scrolls (at most 1000 rows); a click checks what lies under the point first and refuses
  `covered` instead of clicking something else; a step that answered `running` keeps its result
  until `web_wait` collects it. With `headless: false`, the visible Edge keeps rendering when its
  window is covered or nobody views the session.
- `boring-pondnt-d365whsapp` ([docs/D365WHSAPP.md](docs/D365WHSAPP.md)) drives Warehouse
  Management flows for tests: `whs_login`, `whs_screen`, `whs_submit`, `whs_logoff`,
  `whs_run_scenario`, `whs_wait`, `whs_log`. Two backends with the same tools: `app` (the mobile
  app's service protocol, the default) and `form` (the in-browser simulator); results carry their
  `mode` and what was `not_checked`. One session per worker; a lost reply is recovered by a new
  login, never by a blind resend. A readable step log per worker, secrets masked.

Changed:

- The D365 server keeps its own tools only where they redact, parse or guard: `d365_run` runs any
  d365fo.tools command in a warm PowerShell session; the four cross-reference tools are one
  `xref(what=...)`. `d365_status`, `d365_models` and `d365_environment` are removed (use
  `d365_run`). Copilot caches tool lists: restart the MCP server entry after updating.
- Optional gateway: `setup.ps1 -Gateway all` (or a list, `none`, default `keep`) puts the
  specialist services behind one `boring-gateway` entry (MCP Ferry) with four tools; core and
  Knowledge stay direct.
- Fresh installs receive the handbook. Setup writes `.mcp.json` only when its entries change,
  waits for an old task's processes and listeners before replacing them, requires every planned
  listener to belong to its service, stops on startup or index-build failures before publishing
  client entries, and keeps a non-secret recovery record for an interrupted apply or uninstall.
- One lifecycle for process jobs in the core and the D365 server; one helper for results that
  belong to whoever collects them.
- Pond tunnel ([docs/DEVBOX_TUNNEL.md](docs/DEVBOX_TUNNEL.md)): `devbox-d365web` and
  `devbox-d365whsapp` beside `devbox` and `devbox-d365`; `-EnsureTunnel` restarts a dropped tunnel
  from a second scheduled task; other Ponds can be callers through `devbox-callers.json`; an
  optional maintenance forward to the box's sshd; OWNER RIGHTS entries in the credential tree are
  accepted.

Removed:

- Migrating an install from before the handbook (a state `kit` folder): setup refuses it before
  configuring anything and says how to reinstall. The core's `--kit` alias and bootstrap's `kit`
  fields are gone; use `--handbook` and the `handbook` fields.

## 0.2.0rc23 (tag v0.2.0-rc.23) — 2026-10-22

- Kit (`visualstudio.md`): edit your own X++ elements through the solution's
  source-repository path, not the `PackagesLocalDirectory` path a search may
  return; on FileNotFound from an edit tool, retry with the solution-relative
  path or `edit_file`. Observed on a D365 box with Visual Studio 2022.
- INSTALL step 1 starts at the download: where releases are, `curl.exe`, the
  hash check, and why `Invoke-WebRequest` can seem to hang.
- The bundle no longer carries pywin32's Pythonwin and Windows-service host,
  which pondNT never runs.

## 0.2.0rc22 (tag v0.2.0-rc.22) — 2026-10-21

First public release of pondNT, MCP servers for Visual Studio Copilot on a Windows Dynamics 365 development machine, with its working agreement for Copilot.
