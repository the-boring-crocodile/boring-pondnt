# Installing pondNT on a D365 dev box

pondNT gives Copilot in Visual Studio two Windows-only MCP servers and three
independent general services on a Dynamics 365 F&O development box. This
page takes a box from "I have the zip" to "Copilot answers through them".
Windows gets one scheduled task per server under `\pondNT\`, and nothing
else: no Windows service, no registry change, no PATH change.
`.\setup.ps1 -Uninstall` removes the tasks again (step 9).

| Server | Launcher | What it gives Copilot | Needs |
|---|---|---|---|
| core | `boring-pondnt-core.cmd` | files, commands, jobs on this box | nothing |
| source index | `boring-sourcecache-d365.cmd` | search the D365 source, elements, labels | an index file (step 5) |
| d365 | `boring-pondnt-d365.cmd` | build, sync, best practice, results, services, SQL on AxDB | d365fo.tools (step 6) |
| compiled xref (in D365) | `boring-pondnt-d365.cmd` | who calls / uses / extends what | DYNAMICSXREFDB; independent of PowerShell |

Knowledge (notes the agents keep) and Canvas (shared drawings) are the other
two general services. Commands below are for PowerShell (Windows PowerShell
5.1 or 7), one per line. This page also comes as `INSTALL.md` next to the
zip and inside it.

## Before you start

- **Local administrator.** Setup registers elevated and SYSTEM tasks and may
  change Defender. Run it in PowerShell opened with *Run as administrator*.
  Copilot's commands then run elevated, as you.
- **Visual Studio 2022 17.14 or later** with GitHub Copilot. MCP tools work
  only in Copilot's **Agent** mode.
- **Your organisation's Copilot policy.** GitHub Copilot's MCP support can be
  switched off by the organisation or enterprise policy. Then Visual Studio
  offers no MCP tools at all, whatever this page does. Ask whoever manages
  your Copilot licences.
- **Execution policy.** Setup is run with `-ExecutionPolicy Bypass`. An
  execution policy set by group policy takes precedence, and then the script
  does not run. That is for your administrators to decide, not for setup.
- **Unblock the zip.** A downloaded zip carries the Mark of the Web, and every
  file unpacked from it inherits it. Run `Unblock-File` on the zip before
  unpacking (step 1).
- **d365fo.tools comes from the PowerShell Gallery.** Behind a proxy, or with
  the gallery blocked, `Install-Module d365fo.tools` fails. pondNT works
  without it, except the tools that drive builds and syncs (step 6).
- **Disk and time for the source index.** About 6 GB for two label languages
  (21 GB with all ~190), on a data drive, not C:. The build takes 10-35
  minutes with the Defender exclusion, depending on the disk; many times
  longer without it (step 2).

## The short way: setup.ps1

After unpacking (step 1), in an elevated PowerShell window in the unpacked
folder, look first:

    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -WhatIf

It looks at the box (PackagesLocalDirectory, an existing index,
d365fo.tools, the cross-reference database, Defender) and prints a plan. It
changes nothing, skips the live self-check probes and reports readiness as
unverified; it does not need elevation. Then apply, in the elevated window:

    powershell -ExecutionPolicy Bypass -File .\setup.ps1

It asks before each change (`-Yes` answers yes to all but Defender):

- builds the source index if none exists (`-Locales en-US,de`, `-Name`,
  `-Index` to choose; 10-35 minutes with the Defender exclusion, depending on
  the disk; many times longer without it);
- sets up the five servers on 127.0.0.1, each with its own access key and
  scheduled task (`-Services sourcecache,knowledge` to pick, `-SkipServices`
  to skip), and writes their entries into `%USERPROFILE%\.mcp.json`,
  keeping customized and other entries, with a backup first.

Without elevation it stops with "Run this in PowerShell as Administrator."
It never touches Defender unless you agree or add `-AddDefenderExclusion`
(step 2: dangerous), and never installs d365fo.tools (step 6).
[What setup installs](#what-setup-installs) lists every task and folder.
The steps below are what it does, by hand.

## 1. Unpack

    Unblock-File .\boring-pondnt-VERSION-win-x64.zip
    Expand-Archive .\boring-pondnt-VERSION-win-x64.zip C:\tools
    $p = 'C:\tools\boring-pondnt-VERSION-win-x64'

The zip holds one folder; `$p` is that folder, and every command below uses
it. Everything runs from there: Python 3.14 and PowerShell 7 are inside,
nothing else is needed.

## 2. Defender (optional -- dangerous, read first)

**What.** A *process exclusion* tells Microsoft Defender not to scan the
files that one program opens. Here the program is pondNT's own
`python.exe`; every file it reads or writes, anywhere on the box, is no
longer scanned on that access.

**Why.** The source index and the source search read hundreds of thousands
of files. With real-time protection scanning each read, they crawl: on the
first dev box a check over the source took 82 minutes with scanning and
5 minutes without, and the index build was many times slower.

**The danger.** Excluding a process opens a hole in the box's protection:
- a malicious file that pondNT's python opens is not caught at that moment
  (it still is when anything else opens it);
- whoever can replace or start that `python.exe` gets an unscanned program:
  keep its folder writable for administrators only;
- the core server runs commands for Copilot: those are other programs
  and stay scanned, but files the core server itself writes are not;
- your organisation's security policy may forbid it, or report it.

Do it only on a box you may change this way -- a disposable dev box, or with
your security team's OK. Without it everything works, only slower. In an
elevated PowerShell:

    Add-MpPreference -ExclusionProcess "$p\python\python.exe", "$p\python\pythonw.exe"

Narrower alternative: exclude only the index file's folder
(`Add-MpPreference -ExclusionPath J:\pondnt`) -- searching the index gets
fast, building it and reading source files stay scanned.

`pythonw.exe` is the core's interpreter (it runs without a window). Setup asks
about both (default no; `-Yes` never answers it) and `-Uninstall` offers to remove them.

Undo: `Remove-MpPreference -ExclusionProcess "$p\python\python.exe", "$p\python\pythonw.exe"` (and
`-ExclusionPath` for the folder). The self-check (step 3) shows whether the
exclusion is in place. Removing pondNT (step 9) includes undoing this.

**Visual Studio, the X++ build and SQL Server** are a separate matter:
`devbox-defender.ps1` (next to `setup.ps1`) excludes the D365 folders and
tools. It only reports unless you add `-Apply`; `-Undo` processes the
exclusions recorded as owned in its private receipt. A pre-existing exclusion
is left alone. The receipt is written before each change; a failed or
unconfirmed Add/Remove leaves a pending entry and blocks further automatic
changes until an operator reviews the live Defender preference and record.
Neither receipt nor Defender can prove that another administrator did not later adopt
an exclusion, so review live preferences before Undo. Levels: default (Microsoft-signed tools and Microsoft-only
packages; ISV and customer packages stay scanned), `-NoProcesses`, and
`-D365-All` (the whole AOSService folder, Visual Studio, devenv, MSBuild,
SQL Server, IIS -- whatever these processes open goes unscanned). It never
excludes pondNT's python.exe; that is the line above.

    powershell -ExecutionPolicy Bypass -File .\devbox-defender.ps1
    powershell -ExecutionPolicy Bypass -File .\devbox-defender.ps1 -D365-All -Apply

## 3. Check the machine

    & "$p\boring-pondnt-core.cmd" --self-check

prints JSON: shells, long paths, d365fo.tools, the cross-reference
database, Defender -- what works and what is missing. It changes nothing.

With the `-tests` zip you can also run the test suite (about a minute):

    & "$p\run-tests.cmd"

Expect "passed" and a few "skipped" (tests for other platforms). The test
bundle also has `run-services-tests.cmd` for the general services.

## 4. Try a server by hand

Normally you start neither server yourself: setup's logon tasks run them
(step 7). Both answer over HTTP only, configured by `--config`; `--help`
lists the options:

    & "$p\boring-pondnt-core.cmd" --help
    & "$p\boring-pondnt-d365.cmd" serve --help

- The core: `--config` (the HTTP configuration setup writes),
  `--kit` (the folder `bootstrap` serves), `--state-dir` (job output and the
  call log), `--no-call-log`, `--call-budget` and `--self-check`.
- The D365 server, `serve`: `--config`, `--powershell` (Windows PowerShell
  5.1, which d365fo.tools needs), `--sql-server` (default `localhost`, for
  `d365_sql`), `--xref-server`, `--xref-database` (default `DYNAMICSXREFDB`),
  `--xref-driver`, `--xref-stall-seconds`, `--state-dir` and `--no-call-log`.

Setup writes each server's configuration to
`%LOCALAPPDATA%\boring-pondnt\services\<server>\config.json` (loopback
address, port, key file); its task starts the server with that file.

## 5. The source index (once per box)

    & "$p\boring-sourcecache-d365.cmd" build --source J:\AosService\PackagesLocalDirectory --db J:\pondnt\d365.db --name d365scm-10.0.49 --locales en-US,de

- `--source`: the box's PackagesLocalDirectory (`J:\AosService\...` on
  Azure dev boxes, `K:\AosService\...` on older ones -- look for it).
- `--db`: the index file to write, on a large data drive, not C:
  (`Get-PSDrive -PSProvider FileSystem` lists the drives and their free
  space). Its folder is created.
- `--name`: a label for this index, shown in every result and used to pick
  it when a server holds several, e.g. `d365scm-10.0.49` (product and
  version) or the environment's name. The exact version is recorded anyway.
- `--locales`: the label languages you read, e.g. `en-US,de` (about 6 GB;
  all ~190 otherwise: 21 GB on the first dev box).

It takes 10-35 minutes with the Defender exclusion, depending on the disk;
many times longer without it. It runs in the foreground: use a window of its
own. Rebuild after big updates.

## 6. d365fo.tools (for the d365 server)

The d365 server drives builds and syncs through the community module
d365fo.tools. It does not install it. If it is missing, PowerShell-dependent tools say
so; xref still works. Install it once, in an elevated Windows PowerShell
(this needs the PowerShell Gallery; see "Before you start"):

    Install-Module d365fo.tools -Scope AllUsers

## 7. Visual Studio (Copilot)

Visual Studio 2022 17.14+ reads `%USERPROFILE%\.mcp.json`. Setup writes one
entry per server (tips and sign-in quirks in
[Copilot in Visual Studio](https://github.com/the-boring-crocodile/boring-pondnt/blob/main/docs/VISUAL_STUDIO_COPILOT.md)):

    {
      "servers": {
        "boring-pondnt": { "type": "http", "url": "http://127.0.0.1:18513/mcp",
                           "headers": { "Authorization": "Bearer <key>" } },
        "boring-d365": { "type": "http", "url": "http://127.0.0.1:18514/mcp",
                         "headers": { "Authorization": "Bearer <key>" } }
      }
    }

The general services get the same kind of entry
(`boring-sourcecache`, `boring-knowledge`, `boring-canvas`).
Setup replaces old generated stdio entries with the HTTP ones;
an entry you changed by hand stays as it is and is reported, so the tools are
never offered twice. Entries from older releases (`pondnt`, `d365`, `d365-xref`,
`d365-source`) are reported for you to remove; `-Uninstall` removes them.
Do not add new stdio entries: no pondNT server speaks stdio.

Copilot needs **Agent** mode for any MCP tool; in Ask mode it says it cannot
call them. It asks for approval per tool; each server's menu in the tools
picker also offers "Run all tools automatically".

**If tools seem missing,** ask Copilot to list its tools first. Visual
Studio's own built-in tools may not appear in the tools picker. The kit's
`visualstudio.md` describes what Copilot may change in the IDE and when to
restart a session.

**After an update that adds tools to a server:** Visual Studio keeps the tool
list it saw first, even across restarts (a new Canvas tool stayed invisible). In the tools picker, open the server's menu (the small
triangle next to it) and choose **Restart**. Visual Studio 17.14 may then
close without a word: start it again. The new tool is listed, possibly
unticked: tick it.

**After setup, if the tools picker has no "GitHub Copilot" group:** setup
rewrote `.mcp.json`, and Visual Studio restarted its Copilot agent; sometimes
Copilot's own tools do not come back. Restart Visual Studio or sign in to
GitHub again.

Both servers keep running between Visual Studio sessions (jobs survive a
Visual Studio restart); their logs are `service.log` in
`%LOCALAPPDATA%\boring-pondnt\services\core` and `...\d365`. The D365 server runs
elevated (its task's highest run level), as d365fo.tools needs.

## 8. The first request

In Visual Studio, open Copilot Chat, switch to **Agent** mode, start a new
thread and ask:

    Call bootstrap on boring-pondnt and tell me the server version and the kit folder.

Copilot asks to approve the call. The answer names `boring-pondnt-core`, its
version, and the kit folder `%LOCALAPPDATA%\\boring-pondnt\\kit`. The answer
also asks Copilot to read the kit's agreement, housekeeping and persona next.
Whether it does, and whether it takes on the persona's voice, is Copilot's
call: asked only for the bootstrap call, it may report and stop there. Ask it
to follow the bootstrap if you want the whole kit; "Plain, please" switches
the voice off for the session. Nothing on the box changes.

Start every new thread the same way: the kit asks Copilot to call
`bootstrap` at the start of each fresh context. To make that automatic for a
repository, merge the kit's `copilot-instructions.md` into its
`.github/copilot-instructions.md`.

## 9. Remove

In an elevated PowerShell in `$p`, first remove what setup registered:

    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Uninstall -Index J:\pondnt\d365.db

After asking, it stops and unregisters pondNT's scheduled tasks and removes
their `.mcp.json` entries; then it offers to remove the Defender exclusions
for this release's python. Give `-Index` the index file setup used: only then can it
recognise the Sourcecache task, which otherwise stays in place. Tasks you
changed by hand are kept and reported.

Then delete by hand what it lists, as you want: the service data
(keys, notebook, canvases, logs) in `%LOCALAPPDATA%\boring-pondnt\services`,
the kit in `%LOCALAPPDATA%\boring-pondnt\kit`, the index file and its folder,
state and logs in `%LOCALAPPDATA%\boring-pondnt` (unless `--state-dir` said
otherwise), and last the folder `$p`.

## Optional: reaching the box from a Pond

Not part of a normal installation. The servers can also run as services of
a Pond (an MCP host of ours) that reaches the box through an SSH tunnel the
box opens -- how we test pondNT from outside. The tooling ships in
`extras\pond-tunnel\` with its own README; nothing in the steps above needs
it, and nothing of it runs unless you start it.

## Reference

### What setup installs

Five scheduled tasks under `\pondNT\`, each starting one server on
127.0.0.1. Each restarts up to three times, a minute apart, and has no time
limit. `%LOCALAPPDATA%` is that of the person who ran setup.

| Task | Server, port | Runs as | Starts | Elevated | Its folder |
|---|---|---|---|---|---|
| `pondNT boring-pondnt` | core, 18513 | you, through `pythonw.exe` (no window) | at logon | yes (highest run level) | `%LOCALAPPDATA%\boring-pondnt\services\core` |
| `pondNT boring-d365` | D365 server, 18514 | you, through `pythonw.exe` | at logon | yes | `...\services\d365` |
| `pondNT boring-sourcecache` | Sourcecache, 18501 | SYSTEM | at boot | yes | `...\services\sourcecache`; the index file where `-Index` says |
| `pondNT boring-knowledge` | Knowledge, 18512 | SYSTEM | at boot | yes | `...\services\knowledge` (`notebook.db`) |
| `pondNT boring-canvas` | Canvas, 18511 (web page 18510) | SYSTEM | at boot | yes | `...\services\canvas` |

Each folder holds the server's `config.json`, `keys.json` and `service.log`.
Besides the tasks, setup writes:

- `%USERPROFILE%\.mcp.json`: one entry per server, after a backup next to it
  (`.mcp.json.bak-<time>-<id>`);
- `%LOCALAPPDATA%\boring-pondnt\kit`: the kit, copied once;
- the source index, if it builds one;
- Defender process exclusions for this release's `python.exe` and
  `pythonw.exe`, only if you agree.

The core keeps its job output and call log in `%LOCALAPPDATA%\boring-pondnt`.
The Pond tunnel, when used, takes ports 18520 and 18522.

`.\setup.ps1 -Uninstall` removes the `.mcp.json` entries and the tasks it
recognises as its own, and offers to remove the Defender exclusions. It keeps
the folders, the kit and the index, and lists them for you (step 9).

### Why these accounts

The core and the D365 server run as you, because Copilot's commands run in
them and must see your profile, git credentials and PATH; d365fo.tools needs
them elevated. The general services run as SYSTEM, because a task under your
own account does not fire at boot on these VMs; they need no stored password
and show no window.

This installer is limited to the single-administrator disposable development
box. The release and state directory are developer-writable, and scheduled services
run as SYSTEM. This is not an isolation boundary against that developer.
For shared machines, use the separately configured standalone services and a
reviewed service identity instead of assuming this installer supplies one.

### Recognising its own tasks

Setup recognizes existing tasks by their complete service command, working
directory, principal and trigger. A matching name alone is not
ownership. Customized or unrecognized definitions are retained and require
manual review; automatic upgrade refuses them before writing service settings.
Uninstall only considers owned service entries. Supply the existing `-Index`
when removing Sourcecache so its complete command can be verified; otherwise
its task stays in place. Leftover Python processes must match both executable
and complete service command before setup can stop them.

### Access keys

Every server lets in only callers it knows. On this machine that is an
**access key** per server. Setup makes one key per server for you (role
writer, one year; running setup again renews it when fewer than 30 days are
left) and puts it into the server's `.mcp.json` entry as
`Authorization: Bearer ...`. The servers listen on this machine's loopback
interface only, but the key itself is not bound to the device: it is a bearer
secret, and whoever holds it and can reach the port acts with its role. Keep
`.mcp.json` private.

**Keys by hand** (for example for a colleague, or an offsite service over HTTPS):

    boring-service-keys.cmd add --file <folder>\keys.json --subject colleague@box --role reader --days 90
    boring-service-keys.cmd list --file <folder>\keys.json
    boring-service-keys.cmd remove --file <folder>\keys.json --id <id>

Roles are enforced per tool. A reader may read and search files, list and
observe jobs (`job_list`, `job_wait`, `job_output`), and use `d365_status`,
`d365_models`, `d365_results` and the `xref_*` tools. Everything else needs
writer: file writes and patches, `exec`, `job_start`, `job_stop`, and the D365
build, sync, best practice, environment, `d365_job` and `d365_sql` tools. A
reader's `batch` is checked step by step before any step runs; one writer step
refuses the whole batch.

The key is printed once; the file keeps only its fingerprint. Changes take
effect at the next call, no restart. In Visual Studio such a key belongs in a
password input rather than in plain text:
`"headers": {"Authorization": "Bearer ${input:knowledge-key}"}`.

### The kit

The core serves `%LOCALAPPDATA%\boring-pondnt\kit`.
The first setup copies the release's `kit\` there; later releases never
overwrite it. Setup prints which files differ from the new release's kit, for
you to compare and merge by hand (the release's copy stays in its folder;
bootstrap names it `release_kit`). To take a release's kit as it is, rename or
remove that folder and run setup again. `-Uninstall` keeps it.

### The general services on their own

Sourcecache, Knowledge and Canvas are separate applications; the core does
not route or own them. Besides access keys they can let callers in by OAuth
introspection, or both. The release folder has their configuration guide
(`SERVICES_CONFIGURATION.md`) and `examples\`.

They run on the bundled Python runtime through their launchers; a separate
venv is optional. Each runs as its own process with its own config and port.
Provision the issuer and credentials before starting; no authority or
developer enrollment is installed automatically. For example, after
configuring the files:

    boring-knowledge.cmd http --config knowledge.json --db notebook.db --role global
    boring-sourcecache-d365.cmd serve --config source.json --db snapshot-v1.db
    boring-canvas-server.cmd --config canvas.json --state-dir canvas-state

The old Sourcecache launcher is a delegation alias.
