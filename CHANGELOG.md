# Changelog

Notable changes per release, newest first ([Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
[SemVer](https://semver.org/); tags `vX.Y.Z`, candidates `vX.Y.Z-rc.N`).

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
