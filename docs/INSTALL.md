# Installing pondNT on a D365 dev box

pondNT gives Copilot in Visual Studio workspace and D365 tools, three
independent general services, and optional browser and warehouse tools on a
Dynamics 365 F&O development box. This page takes a box from download to a
working connection. The recommended layout keeps core and Knowledge direct
and puts the other selected services behind Ferry.
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

After downloading and unpacking (step 1), in an elevated PowerShell window in the unpacked
folder, look first:

    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -WhatIf -Gateway all

It looks at the box (PackagesLocalDirectory, an existing index,
d365fo.tools, the cross-reference database, Defender) and prints a plan. It
changes nothing, skips the live self-check probes and reports readiness as
unverified; it does not need elevation. Then apply, in the elevated window:

    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Gateway all

It asks before each change (`-Yes` answers yes to all but Defender):

- builds the source index if none exists (`-Locales en-US,de`, `-Name`,
  `-Index` to choose; 10-35 minutes with the Defender exclusion, depending on
  the disk; many times longer without it);
- sets up the five default services on 127.0.0.1, each with its own access
  key and scheduled task (`-Services` chooses services, `-SkipServices`
  skips them);
- with the recommended `-Gateway all`, adds Ferry and publishes three client
  entries: `boring-pondnt`, `boring-knowledge` and `boring-gateway`. The
  gateway offers `service_list`, `service_describe`, `service_read` and
  `service_call`; Sourcecache, Canvas and D365 are behind it. Browser/WHS
  join them when selected. Backend tasks, ports and keys remain separate;
- updates `%USERPROFILE%\.mcp.json` only when its entries or credentials
  need to change, with a backup first. Customized entries are retained and
  reported for review; unrelated entries stay in place.

**Why this layout?** Core and notes are useful throughout a conversation.
The other tools are specialised: Copilot can discover the relevant service
and its tool descriptions when the task calls for them. Ferry keeps those
services available without putting every tool into the initial list. When
that list is sent to the model, its descriptions and schemas consume input
tokens, even for tools never called. On-demand discovery can reduce this
up-front context and token cost; the actual saving depends on the client,
caching and task. Ferry adds discovery calls of its own.
[Gateway benefits and trade-offs](https://github.com/the-boring-crocodile/boring-services/blob/main/mcp-ferry/README.md#gateways-optional)
explains the choice. Direct entries remain available if they suit your client better.

`-Gateway all` means all services in this run's selection except core and
Knowledge. On an existing installation, pass the complete intended
`-Services` list, including browser/WHS opt-ins you want to keep behind Ferry.
For an installation using all seven services, that selection is
`-Services 'sourcecache,knowledge,canvas,core,d365,d365web,d365whsapp'`.
Retain the correct index path and existing browser environment/company
configuration; review the preview before applying. These commands use the
scheduled-task installer. An existing manual launcher needs its own reviewed
configuration change rather than a change to Windows startup policy.

Alternatives: `-Gateway d365,canvas` carries only those selected services;
`-Gateway none` restores direct client entries and retires the gateway.
Omitting the flag uses `keep`, preserving existing routing. The recommendation
is explicit in the commands above; the installer does not silently convert
an existing setup.

Without elevation it stops with "Run this in PowerShell as Administrator."
It never touches Defender unless you agree or add `-AddDefenderExclusion`
(step 2: dangerous), and never installs d365fo.tools (step 6).
[What setup installs](#what-setup-installs) lists every task and folder.
The steps below are what it does, by hand.

## 1. Download, check and unpack

Releases are on
[GitHub](https://github.com/the-boring-crocodile/boring-pondnt/releases);
candidates are marked as pre-releases. Each has the zip and a `.sha256` file.
`TAG` below is the release's tag (`v0.2.0-rc.23`), `VERSION` the version in
the file name (`0.2.0rc23`). `curl.exe` comes with Windows:

    $u = 'https://github.com/the-boring-crocodile/boring-pondnt/releases/download/TAG'
    curl.exe -LO "$u/boring-pondnt-VERSION-win-x64.zip"
    curl.exe -LO "$u/boring-pondnt-VERSION-win-x64.zip.sha256"
    (Get-FileHash .\boring-pondnt-VERSION-win-x64.zip -Algorithm SHA256).Hash.ToLower() -eq (Get-Content .\boring-pondnt-VERSION-win-x64.zip.sha256).Split(' ')[0]

The last line must print `True`. `Invoke-WebRequest` works too, but in
Windows PowerShell 5.1 it is very slow on a file this size unless
`$ProgressPreference = 'SilentlyContinue'` is set first. A browser may warn
that the zip is not commonly downloaded, or Defender may look at it closely:
it holds Python, PowerShell 7 and their programs. Keep it only if the hash
matches. Then:

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
  `--handbook` (the folder `bootstrap` serves), `--state-dir` (job output and the
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

Visual Studio 2022 17.14+ reads `%USERPROFILE%\.mcp.json`. With the
recommended `-Gateway all` layout, setup publishes these three entries
(tips and sign-in quirks in [Copilot in Visual Studio](VISUAL_STUDIO_COPILOT.md)).
This is the shape to expect; setup supplies a distinct key for each entry:

    {
      "servers": {
        "boring-pondnt": { "type": "http", "url": "http://127.0.0.1:18513/mcp",
                           "headers": { "Authorization": "Bearer <core-key>" } },
        "boring-knowledge": { "type": "http", "url": "http://127.0.0.1:18512/mcp",
                             "headers": { "Authorization": "Bearer <knowledge-key>" } },
        "boring-gateway": { "type": "http", "url": "http://127.0.0.1:18517/mcp",
                           "headers": { "Authorization": "Bearer <gateway-key>" } }
      }
    }

Sourcecache, Canvas, D365 and selected browser/WHS backends do not have
separate client entries in this layout. Ask Copilot to use Ferry's
`service_list`, then `service_describe` for the service it needs. With direct
routing, each service instead has its own HTTP entry and key.
Setup replaces old generated stdio entries with the HTTP ones;
an entry you changed by hand stays as it is and is reported, so the tools are
never offered twice. Entries from older releases (`pondnt`, `d365`, `d365-xref`,
`d365-source`) are reported for you to remove; `-Uninstall` removes them.
Do not add new stdio entries: no pondNT server speaks stdio.

Copilot needs **Agent** mode for any MCP tool; in Ask mode it says it cannot
call them. It asks for approval per tool; each server's menu in the tools
picker also offers "Run all tools automatically".

**If tools seem missing,** ask Copilot to list its tools first. Visual
Studio's own built-in tools may not appear in the tools picker. The handbook's
`visualstudio.md` describes what Copilot may change in the IDE and when to
restart a session.

**After an update that adds tools to a server:** Visual Studio keeps the tool
list it saw first, even across restarts (a new Canvas tool stayed invisible). In the tools picker, open the server's menu (the small
triangle next to it) and choose **Restart**. Visual Studio 17.14 may then
close without a word: start it again. The new tool is listed, possibly
unticked: tick it.

Setup writes `.mcp.json` only when client entries or credentials need to change.
An unchanged setup or a backend-only Ferry change preserves its bytes and
modification time without making a backup. Changes inside a backend do not
require a new client entry while the gateway URL and key stay the same.

**After setup, if the tools picker has no "GitHub Copilot" group:** a needed
`.mcp.json` update restarted Visual Studio's Copilot agent; sometimes
Copilot's own tools do not come back. Restart Visual Studio or sign in to
GitHub again.

Both servers keep running between Visual Studio sessions (jobs survive a
Visual Studio restart); their logs are `service.log` in
`%LOCALAPPDATA%\boring-pondnt\services\core` and `...\d365`. The D365 server runs
elevated (its task's highest run level), as d365fo.tools needs.

## 8. The first request

In Visual Studio, open Copilot Chat, switch to **Agent** mode, start a new
thread and ask:

    Call bootstrap on boring-pondnt and tell me the server version and the handbook folder.

Copilot asks to approve the call. The answer names `boring-pondnt-core`, its
version, and the handbook folder `%LOCALAPPDATA%\\boring-pondnt\\handbook`. The answer
also asks Copilot to read the handbook's agreement, housekeeping and persona next.
Whether it does, and whether it takes on the persona's voice, is Copilot's
call: asked only for the bootstrap call, it may report and stop there. Ask it
to follow the bootstrap if you want the whole handbook; "Plain, please" switches
the voice off for the session. Nothing on the box changes.

With the recommended layout, also ask:

    Use boring-gateway's service_list and describe the D365 service. Report what is available; do not run a build or change data.

This checks discovery through Ferry. A listening gateway alone does not prove
that its backends answer; inspect their reported availability. Core and
Knowledge should still appear as direct servers.

Start every new thread the same way: the handbook asks Copilot to call
`bootstrap` at the start of each fresh context. To make that automatic for a
repository, merge the handbook's `copilot-instructions.md` into its
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
the handbook in `%LOCALAPPDATA%\boring-pondnt\handbook`, the index file and its folder,
state and logs in `%LOCALAPPDATA%\boring-pondnt` (unless `--state-dir` said
otherwise), and last the folder `$p`.

### Recovery after an interrupted setup

Setup records each apply/uninstall attempt under
`%LOCALAPPDATA%\boring-pondnt\setup-attempts\`. The JSON records contain
paths, chosen services/routing, the last saved phase and completed service
steps. They contain no access keys. A setup killed between saves may have
completed its current step; inspect the actual tasks/listeners before retrying.

A failed startup stops client publication and old-key retirement. Some
services may already have restarted. The existing client file stays until
publication succeeds; after publication, a cleanup failure leaves the new
client in place. Setup reports failure and the recovery-record path.

Read that record's `retry_preview` command first. It includes the full service
selection, explicit routing, index and client path, so a retry retains opt-in
backends even after a partial routing change. Run the preview, resolve any
ownership/conflict messages, then use `retry_apply` to retry with normal prompts.
Keep the named release folder until recovery is complete. Existing notebook,
canvas, handbook and index data stay in their original locations.

## Optional: reaching the box from a Pond

Not part of a normal installation. The servers can also run as services of
a Pond (an MCP host of ours) that reaches the box through an SSH tunnel the
box opens -- how we test pondNT from outside. The tooling ships in
`extras\pond-tunnel\` with its own README; nothing in the steps above needs
it, and nothing of it runs unless you start it.

## Reference

### What setup installs

This section describes the standard **Task Scheduler installation** created
by `setup.ps1`. The account and trigger columns below are not properties of
HTTP or Ferry routing: Ferry forwards calls to separately running backends;
it does not start or supervise them. If an operator uses a separate manual
launcher, its processes run under the launching account and start when that
launcher runs. Such a launcher has its own recovery arrangements; this setup
command manages scheduled tasks.

The recommended base setup has six scheduled tasks: five services plus
Ferry. Selecting both browser/WHS opt-ins makes eight. Without Ferry, the
base setup has five. Tasks live under `\pondNT\`, each starting one server on
127.0.0.1. Each restarts up to three times, a minute apart, and has no time
limit. `%LOCALAPPDATA%` is that of the person who ran setup.

| Task | Server, port | Runs as | Starts | Elevated | Its folder |
|---|---|---|---|---|---|
| `pondNT boring-pondnt` | core, 18513 | you, through `pythonw.exe` (no window) | at logon | yes (highest run level) | `%LOCALAPPDATA%\boring-pondnt\services\core` |
| `pondNT boring-d365` | D365 server, 18514 | you, through `pythonw.exe` | at logon | yes | `...\services\d365` |
| `pondNT boring-d365web` | D365 browser, 18516 (opt-in) | you, through `pythonw.exe` | at logon | yes | `...\services\d365web` |
| `pondNT boring-d365whsapp` | WHS app/form, 18515 (opt-in) | you, through `pythonw.exe` | at logon | yes | `...\services\d365whsapp` |
| `pondNT boring-sourcecache` | Sourcecache, 18501 | SYSTEM | at boot | yes | `...\services\sourcecache`; the index file where `-Index` says |
| `pondNT boring-knowledge` | Knowledge, 18512 | SYSTEM | at boot | yes | `...\services\knowledge` (`notebook.db`) |
| `pondNT boring-canvas` | Canvas, 18511 (web page 18510) | SYSTEM | at boot | yes | `...\services\canvas` |
| `pondNT boring-gateway` | the gateway (boring-mcp-ferry), 18517; only with `-Gateway` | SYSTEM | at boot | yes | `...\services\gateway` (`ferry.json`, one `<service>.headers.json` per service behind it: that service's key) |

Each folder holds the server's `config.json`, `keys.json` and `service.log`.
Besides the tasks, setup writes:

- `%USERPROFILE%\.mcp.json`: three entries for the recommended layout,
  or one per directly exposed server. A needed change gets a backup next to
  it (`.mcp.json.bak-<time>-<id>`); an unchanged file is left alone;
- `%LOCALAPPDATA%\boring-pondnt\handbook`: the handbook, copied once;
- the source index, if it builds one;
- Defender process exclusions for this release's `python.exe` and
  `pythonw.exe`, only if you agree.

The core keeps its job output and call log in `%LOCALAPPDATA%\boring-pondnt`.
The optional Pond tunnel takes 18520/18522 for core/D365 and 18524/18526 for
the warehouse/browser servers.

`.\setup.ps1 -Uninstall` removes the `.mcp.json` entries and the tasks it
recognises as its own, and offers to remove the Defender exclusions. It keeps
the folders, the handbook and the index, and lists them for you (step 9).

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
left). A directly exposed server's key goes into its `.mcp.json` entry as
`Authorization: Bearer ...`. A backend behind Ferry instead has its key in
`services\gateway\<service>.headers.json`; the client holds Ferry's own key. The servers listen on this machine's loopback
interface only, but the key itself is not bound to the device: it is a bearer
secret, and whoever holds it and can reach the port acts with its role. Keep
`.mcp.json` private.

**Keys by hand** (for example for a colleague, or an offsite service over HTTPS):

    boring-service-keys.cmd add --file <folder>\keys.json --subject colleague@box --role reader --days 90
    boring-service-keys.cmd list --file <folder>\keys.json
    boring-service-keys.cmd remove --file <folder>\keys.json --id <id>

Roles are enforced per tool. A reader may read and search files, list and
observe jobs (`job_list`, `job_wait`, `job_output`), and use `d365_results` and
`xref`. Everything else needs writer: file writes and patches, `exec`,
`job_start`, `job_stop`, and the D365 build, sync, best practice, `d365_run`,
`d365_job` and `d365_sql` tools. A
reader's `batch` is checked step by step before any step runs; one writer step
refuses the whole batch.

The key is printed once; the file keeps only its fingerprint. Changes take
effect at the next call, no restart. In Visual Studio such a key belongs in a
password input rather than in plain text:
`"headers": {"Authorization": "Bearer ${input:knowledge-key}"}`.

### The handbook

An install from before the handbook (a state `kit` folder) is not migrated:
setup refuses before configuring anything. Uninstall it with its own setup
(`-Uninstall`), move `%LOCALAPPDATA%\boring-pondnt\kit` aside (copy your own
edits into the handbook later), then run this setup.

The core serves `%LOCALAPPDATA%\boring-pondnt\handbook`.
The first setup copies the release's `handbook\` there; later releases never
overwrite it. Setup prints which files differ from the new release's handbook, for
you to compare and merge by hand (the release's copy stays in its folder;
bootstrap names it `release_handbook`). To take a release's handbook as it is, rename or
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
