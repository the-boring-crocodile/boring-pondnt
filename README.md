# pondNT

Part of the [Pond family](https://github.com/the-boring-crocodile/boring-pond/blob/main/README.md).
All of it — code, tests, docs — was written by AI (OpenAI's models and Anthropic's Claude); Wald decided what to build, tested it, ran it and had the last word ([all of it is AI](https://github.com/the-boring-crocodile/boring-pond/blob/main/README.md#all-of-it-is-ai)).

Small MCP servers that let GitHub Copilot in Visual Studio work on a
Dynamics 365 F&O development box: a thin, well-behaved layer between the
agent and full access to that machine, plus knowledge Visual Studio does
not have.

**Choose your working setup:** [Working with pondNT](docs/WORKING_WITH_PONDNT.md)
walks through one Azure dev VM, a Linux workspace connected to Windows,
a shared knowledge base, and local Copilot alongside a remote assistant.
It shows how browser and warehouse tools fit into a development-and-test loop.

**What Copilot can do with it:**

- build a model and read the compiler and best-practice errors as rows
  (`d365_build`, `d365_best_practice`, `d365_results`);
- run a database sync, module or full (`d365_db_sync`);
- find references from Visual Studio's cross-reference database: who calls
  a method, who uses a table, which classes extend a class (`xref`);
- search the standard and your own source, elements and labels in a
  prepared index instead of a crawl over hundreds of thousands of files
  (the source cache);
- run SQL on AxDB (`d365_sql`);
- run commands and long jobs, and read and edit files, on the box;
- work through D365 browser forms and warehouse receiving screens (`web_*`, `whs_*`).

**What it costs:**

- a dev box you can afford to lose: the agent may break it, so only a
  disposable one, with no live data, no data you cannot afford to lose and
  no connection to production;
- local administrator rights, and an install whose source index takes 10-35
  minutes with the Defender exclusion, depending on the disk; many times
  longer without it;
- Copilot's commands run elevated, as you;
- the D365 server holds a writer key: Copilot can change AxDB, build, sync
  and restart the environment ([who can do what](docs/THREAT_MODEL.md));
- optionally, a Defender exclusion for pondNT's python, which opens a hole
  in the box's protection ([what it means](docs/INSTALL.md#2-defender-optional----dangerous-read-first)).

**Why:** Copilot in Visual Studio spends many slow calls and many tokens on
work that a few bounded, well-designed tools answer at once. Working in the
Pond showed the difference. The aim is fewer and smaller Copilot calls, so
better models become affordable for ordinary work. That is still a
hypothesis: it has not been measured in a way worth publishing.

The handbook can also give Copilot a voice (`handbook/persona.md`); "Plain, please"
switches it off for the session.

Install: [docs/INSTALL.md](docs/INSTALL.md). **Recommended: use Ferry with
`setup.ps1 -Gateway all`.** Core and Knowledge remain directly available;
the other selected services are discovered and called through the gateway.

## What runs on the box

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/pondnt-dark.svg">
  <img alt="pondNT on a disposable D365 dev box: Copilot in Visual Studio has three MCP entries, core (files, commands and long jobs, running as you, elevated), Knowledge (the agent's notebook, as SYSTEM) and Ferry (a gateway as SYSTEM with four tools for the rest). Through Ferry: the D365 server (build, sync, SQL, xref, as you, elevated; it answers to a writer key, which Ferry holds), Sourcecache and Canvas (as SYSTEM), and the opt-in browser and warehouse servers (as you, elevated) that work D365 forms and warehouse screens over HTTPS. The D365 server and the browser servers act on the D365 environment: AOS, AxDB and source." src="docs/pondnt.svg">
</picture>

The recommended layout gives Copilot three MCP entries: core, Knowledge
and Ferry. Core keeps the workspace tools at hand; Knowledge keeps the
agent's notes available. Ferry exposes four discovery and call tools for
Sourcecache, Canvas, D365 and any selected browser or warehouse server.
The agent discovers those specialist tools when needed, keeping the initial
tool list small. Tool descriptions and schemas consume input tokens when sent
to the model; loading them on demand can reduce that overhead and its cost.
The saving depends on the client's discovery and caching behaviour and the
task. This asks the model to take an extra discovery step;
[direct connections remain available](docs/INSTALL.md#the-short-way-setupps1).

Use `setup.ps1 -Gateway all` to choose this layout. Browser and warehouse
servers remain opt-in: the flag routes selected services, it does not select
or configure every optional service. Each server still runs separately on
127.0.0.1 with its own access key. **Ferry forwards calls; it does not host
or start the backend processes.** Setup creates the keys and scheduled tasks.

The following accounts and start times describe the **standard scheduled
installation created by `setup.ps1`**:

| Server | Port | Does | Runs as | Starts |
|---|---|---|---|---|
| boring-pondnt-core | 18513 | Files, commands, jobs | you, elevated | at logon |
| boring-pondnt-d365 | 18514 | Build, sync, SQL, xref | you, elevated | at logon |
| boring-pondnt-d365web | 18516 | D365 browser forms | you, elevated | at logon |
| boring-pondnt-d365whsapp | 18515 | Warehouse app protocol or form simulator | you, elevated | at logon |
| Ferry (recommended) | 18517 | Discover and call the configured backends | SYSTEM | at boot |
| Sourcecache | 18501 | D365 source, elements, labels | SYSTEM | at boot |
| Knowledge | 18512 | Notes the agents keep | SYSTEM | at boot |
| Canvas | 18511 (web page 18510) | Shared drawings | SYSTEM | at boot |

These are this box's ports. On a Linux site the same services use their own
(Sourcecache 18513 there, where 18513 is the core here): the
[services README](https://github.com/the-boring-crocodile/boring-services/blob/main/README.md#before-the-first-install).

With an operator-maintained manual launcher instead, the servers run under
the account that launches them and start when that launcher is run. An elevated
desktop CMD can start the whole stack as the person; those processes do not
become SYSTEM tasks or automatically start at boot/logon. Ports, access keys
and Ferry routing are independent of how the processes are started. The
release's standard installer manages scheduled tasks; a custom manual launcher
has its own startup and recovery arrangements.

The optional Pond tunnel uses 18520 (core), 18522 (D365), 18524 (warehouse)
and 18526 (browser).

**Target:** a rebuildable Azure VM (Windows Server, D8v5) with a Dynamics 365
Supply Chain Management development environment; Copilot in Visual Studio on
the same host is the client.

## Shape

Four pondNT servers plus Sourcecache, Knowledge and Canvas, with Ferry
recommended in front of the selected backends except core and Knowledge. Each server keeps its own process and access
key. The general services can instead run elsewhere over HTTPS with OAuth;
the pondNT servers can also be reached by a Pond through a tunnel.

- **`boring-pondnt-core`**: a general Windows toolkit -- bootstrap, files
  (text that keeps encoding, BOM and line endings; raw bytes), exec, jobs,
  batch; a call log and `--self-check`. Scheduled setup runs it at logon as the person,
  PowerShell 7 shipped with it.
  Contract: [docs/FOUNDATION_SPEC.md](docs/FOUNDATION_SPEC.md).
- **`boring-pondnt-d365`**: typed tools on d365fo.tools --
  build, compiler errors as data, DB sync, best practice, restart -- on
  Windows PowerShell 5.1, `d365_run` for d365fo.tools commands in a warm session,
  `d365_sql` for SQL on AxDB, and one read-only `xref` tool on DYNAMICSXREFDB
  (names, references, hierarchy).
  Docs: [docs/D365_SERVICE.md](docs/D365_SERVICE.md).
- **`boring-pondnt-d365web`** and **`boring-pondnt-d365whsapp`**: browser automation
  and warehouse receiving tools. WHS selects the real protocol (`app`) or the
  browser form simulator (`form`); the result identifies the mode and what it
  could not check. See [browser tools](docs/D365WEB.md) and
  [warehouse tools](docs/D365WHSAPP.md).
- **`boring-sourcecache-d365`** (boring-services): full-text source search,
  elements and labels over immutable snapshots. Knowledge and Canvas are
  also independent services; configure each directly:
  boring-services `docs/CONFIGURATION.md`.

**What kind of project this is:** a port of proven design, not new design.
Bounded results, version-hashed edits, jobs, batch, outcomes as data and a
bootstrap handoff are proven in the Boring Crocodile MCP; the source index
is proven on the VM. The new work is Windows and Copilot.

D365 development happens on dev VMs, so that is where pondNT is tested: a
release's full suite runs natively on a local D365 dev VM before it is
published, and the release page carries that run's summary. Daily work on an
Azure D365 dev VM comes after a release and is reported, not acceptance;
[EVIDENCE](docs/EVIDENCE.md) says which release got what. Hosted CI runs the
portable tests on Linux; a hosted Windows runner has no D365, so it could
add only the generic Windows tests. Pieces that prove better than the
Pond's own flow back into the family.

## Documents

- [Working with pondNT](docs/WORKING_WITH_PONDNT.md) -- use cases, connections and a development loop
- [docs/INSTALL.md](docs/INSTALL.md) -- installing on a D365 dev box
- [docs/FOUNDATION_SPEC.md](docs/FOUNDATION_SPEC.md) -- the core's contract
- [docs/D365_SERVICE.md](docs/D365_SERVICE.md), [docs/XREF_SERVICE.md](docs/XREF_SERVICE.md) -- the D365 server and its xref tools
- [docs/VISUAL_STUDIO_COPILOT.md](docs/VISUAL_STUDIO_COPILOT.md) -- Copilot in Visual Studio, learned the hard way
- [docs/EVIDENCE.md](docs/EVIDENCE.md) -- what has been tested where, and what has not
- [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) -- who can do what on the box, what pondNT enforces and what it cannot
- [docs/CLIENTS.md](docs/CLIENTS.md) -- VS Code, Claude Code and Claude Desktop on the same box (untested recipes)
- [docs/DEVBOX_TUNNEL.md](docs/DEVBOX_TUNNEL.md) -- optional Pond tunnel and dev box helpers
- [CHANGELOG.md](CHANGELOG.md) -- what changed

## Building and contributing

One codebase, one entry point per server, shared modules. Built in the Pond
on Linux; Windows behaviour is tested where it runs.

`src/boring_pondnt/`: `disk` (paths, bounded reads, in-place edits and exclusive creation),
`textfiles` (format layer: encodings, BOM, newlines), `windows_files`, `listing`, `binfiles`,
`core_file_tools`, `execs`, `jobs`, `background`, `selfcheck`, `server` (boring-pondnt-core and batch),
`d365`, `d365_tools` and `d365_server` (boring-pondnt-d365), `xref` (query module) and
`xref_tools` (registrations), `sqlserver` (SQL access for d365 and xref),
`setup_services` and `publish_config` (setup's helpers).
Process trees (Job Objects on Windows, process groups on POSIX) come from
`boring_service_kit.process`.
Sourcecache, Knowledge and Canvas come from boring-services.
`boring-service-kit` (outcomes, call log, tool conventions, process trees, the Pond-service
side), `boring-registry` and `boring-file-core` come from Pond Common releases;
the release build takes their wheels with `--common-wheels`. `tests/corpus.py` is the synthetic D365 corpus. `handbook/` holds the working agreement `bootstrap` serves.

Tests in `tests/`. `requirements.lock` and `requirements-tests.lock` pin
everything the release ships with hashes (`scripts/lock.py`); the one check is
`python scripts/check.py --common-wheels <Common dist> --services-root <boring-services>`.
CI selects the bundled Services source at the commit in `.services-ref`;
use that checkout for the command above and the release build. Update the ref
when deliberately changing the bundled services, after checking them with
pondNT's Common selection.
`scripts/build.py` builds the Windows release zip. See also
[CONTRIBUTING.md](CONTRIBUTING.md).

## Related

- [boring-crocodile](https://github.com/the-boring-crocodile/boring-crocodile/blob/main/README.md):
  the Pond's MCP server on Linux. The design and the contracts carry over --
  outcome codes, patch semantics, jobs, batch -- and the shared service side
  is shared on purpose, through
  [Pond Common](https://github.com/the-boring-crocodile/boring-pond-common/blob/main/README.md).
- d365fo.tools (PowerShell Gallery, d365collaborative): the D365 layer the
  D365 server wraps.

## Licence

Code under the GNU AGPL, version 3 or later; documentation under CC BY-SA
4.0. The collaboration handbook (`handbook/`) is public domain (CC0). What you make
with the tools is yours. Details and the exact terms:
[COPYING.md](COPYING.md).
