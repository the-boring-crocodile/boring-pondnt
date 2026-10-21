# pondNT

Part of the [Pond family](https://github.com/the-boring-crocodile/boring-pond/blob/main/README.md).
All of it — code, tests, docs — was written by AI (OpenAI's models and Anthropic's Claude); Wald decided what to build, tested it, ran it and had the last word ([all of it is AI](https://github.com/the-boring-crocodile/boring-pond/blob/main/README.md#all-of-it-is-ai)).

Small MCP servers that let GitHub Copilot in Visual Studio work on a
Dynamics 365 F&O development box: a thin, well-behaved layer between the
agent and full access to that machine, plus knowledge Visual Studio does
not have.

**What Copilot can do with it:**

- build a model and read the compiler and best-practice errors as rows
  (`d365_build`, `d365_best_practice`, `d365_results`);
- run a database sync, module or full (`d365_db_sync`);
- find references from Visual Studio's cross-reference database: who calls
  a method, who uses a table, which classes extend a class (`xref_*`);
- search the standard and your own source, elements and labels in a
  prepared index instead of a crawl over hundreds of thousands of files
  (the source cache);
- run SQL on AxDB (`d365_sql`);
- run commands and long jobs, and read and edit files, on the box.

**What it costs:**

- local administrator rights, and an install whose source index takes 10-35
  minutes with the Defender exclusion, depending on the disk; many times
  longer without it;
- Copilot's commands run elevated, as you;
- the D365 server holds a writer key: Copilot can change AxDB, build, sync
  and restart the environment;
- optionally, a Defender exclusion for pondNT's python, which opens a hole
  in the box's protection ([what it means](docs/INSTALL.md#2-defender-optional----dangerous-read-first)).

**Why:** Copilot in Visual Studio spends many slow calls and many tokens on
work that a few bounded, well-designed tools answer at once. Working in the
Pond showed the difference. The aim is fewer and smaller Copilot calls, so
better models become affordable for ordinary work. That is a hypothesis
until measured, and we say so rather than guess.

The kit can also give Copilot a voice (`kit/persona.md`); "Plain, please"
switches it off for the session.

Install: [docs/INSTALL.md](docs/INSTALL.md).

## What runs on the box

Copilot in Visual Studio calls each server itself over HTTP on 127.0.0.1,
with that server's own access key; the core routes nothing. Every server
listens on this box only. Setup creates the keys and the scheduled tasks.

| Server | Port | Does | Runs as | Starts |
|---|---|---|---|---|
| boring-pondnt-core | 18513 | Files, commands, jobs | you, elevated | at logon |
| boring-pondnt-d365 | 18514 | Build, sync, SQL, xref | you, elevated | at logon |
| Sourcecache | 18501 | D365 source, elements, labels | SYSTEM | at boot |
| Knowledge | 18512 | Notes the agents keep | SYSTEM | at boot |
| Canvas | 18511 (web page 18510) | Shared drawings | SYSTEM | at boot |

The Pond tunnel, when used, takes 18520 and 18522.

**Target:** a rebuildable Azure VM (Windows Server, D8v5) with a Dynamics 365
Supply Chain Management development environment; Copilot in Visual Studio on
the same host is the client.

## Shape

Two Windows-only servers plus three general services. On the box all five
speak HTTP on loopback, each with its own access key. The general services
can instead run elsewhere over HTTPS with OAuth; the two Windows servers can
also be reached by a Pond through a tunnel.

- **`boring-pondnt-core`**: a general Windows toolkit -- bootstrap, files
  (text that keeps encoding, BOM and line endings; raw bytes), exec, jobs,
  batch; a call log and `--self-check`. Run at logon as the person,
  PowerShell 7 shipped with it.
  Contract: [docs/FOUNDATION_SPEC.md](docs/FOUNDATION_SPEC.md).
- **`boring-pondnt-d365`**: typed tools on d365fo.tools --
  build, compiler errors as data, DB sync, best practice, restart -- on
  Windows PowerShell 5.1, `d365_sql` for SQL on AxDB, and four
  read-only xref tools on DYNAMICSXREFDB (names, references, hierarchy).
  Docs: [docs/D365_SERVICE.md](docs/D365_SERVICE.md).
- **`boring-sourcecache-d365`** (boring-services): full-text source search,
  elements and labels over immutable snapshots. Knowledge and Canvas are
  also independent services; configure each directly:
  boring-services `docs/CONFIGURATION.md`.

**What kind of project this is:** a port of proven design, not new design.
Bounded results, version-hashed edits, jobs, batch, outcomes as data and a
bootstrap handoff are proven in the Boring Crocodile MCP; the source index
is proven on the VM. The new work is Windows and Copilot.

D365 development happens on dev VMs, so that is where pondNT is tested:
every release's full suite (`ci/guest-run.ps1`) in a disposable Windows
guest, then daily work on a real Azure D365 dev VM. Hosted CI runs the
portable tests on Linux; a hosted Windows runner has no D365, so it could
add only the generic Windows tests. Pieces that prove better than the
Pond's own flow back into the family.

## Documents

- [docs/INSTALL.md](docs/INSTALL.md) -- installing on a D365 dev box
- [docs/FOUNDATION_SPEC.md](docs/FOUNDATION_SPEC.md) -- the core's contract
- [docs/D365_SERVICE.md](docs/D365_SERVICE.md), [docs/XREF_SERVICE.md](docs/XREF_SERVICE.md) -- the D365 server and its xref tools
- [docs/VISUAL_STUDIO_COPILOT.md](docs/VISUAL_STUDIO_COPILOT.md) -- Copilot in Visual Studio, learned the hard way
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
the release build takes their wheels with `--common-wheels`. `tests/corpus.py` is the synthetic D365 corpus. `kit/` holds the working agreement `bootstrap` serves.

Tests in `tests/`. `requirements.lock` and `requirements-tests.lock` pin
everything the release ships with hashes (`scripts/lock.py`); the one check is
`python scripts/check.py --common-wheels <Common dist> --services-root <boring-services>`.
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
4.0. The collaboration kit (`kit/`) is public domain (CC0). What you make
with the tools is yours. Details and the exact terms:
[COPYING.md](COPYING.md).
