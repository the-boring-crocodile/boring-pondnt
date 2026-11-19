# Installing pondNT on a D365 dev box

For disposable D365 development boxes only: no live data, no data you
cannot afford to lose, no connection to production
([who can do what](THREAT_MODEL.md)).

pondNT gives Copilot in Visual Studio workspace and D365 tools, three
independent general services, and optional browser, warehouse and data tools on a
Dynamics 365 F&O development box. This page takes a box from download to a
working connection. The recommended layout keeps core and Knowledge direct
and puts the other selected services behind Ferry.
Windows gets one scheduled task per server under `\pondNT\`: no Windows
service, no registry change, no PATH change. `.\setup.ps1 -Uninstall`
removes the tasks again (step 9).

| Server | Started by | What it gives Copilot | Needs |
|---|---|---|---|
| core | `boring-pondnt-core.cmd` | files, commands, jobs on this box | nothing |
| d365 | `boring-pondnt-d365.cmd` | build, sync, best practice, results, services, SQL on AxDB; read-only xref (who calls / uses / extends what) | d365fo.tools (step 6); DYNAMICSXREFDB for xref |
| source index (Sourcecache) | `boring-sourcecache-d365.cmd` | search the D365 source, elements, labels | an index file (step 5) |
| Knowledge | `boring-knowledge.cmd` | notes the agents keep | nothing |
| Canvas | `boring-canvas-server.cmd` | shared drawings, with a web page | nothing |
| d365web (opt-in) | `python -m boring_pondnt.d365web_server` | D365 browser forms | a signed-in Edge ([D365WEB.md](D365WEB.md)) |
| d365whsapp (opt-in) | `python -m boring_pondnt.d365whsapp_server` | warehouse app protocol or form simulator | a warehouse worker setup ([D365WHSAPP.md](D365WHSAPP.md)) |
| d365data (opt-in) | `python -m boring_pondnt.d365data_server` | OData and DMF packages | its own Entra app, limited D365 user, DPAPI secret and private package folder ([D365DATA.md](D365DATA.md#install)) |
| Ferry (recommended) | `boring-mcp-ferry.cmd` | one entry that discovers and calls the services behind it | nothing |

Commands below are for PowerShell (Windows PowerShell
5.1 or 7), one per line. This page also comes as `INSTALL.md` next to the
zip and inside it.

## Before you start

- **Local administrator.** Setup registers person/logon tasks and may
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
- **Long paths.** D365 source paths pass Windows' old 260-character limit.
  pondNT checks the setting and never changes it: `--self-check` fails and
  the core's `bootstrap` warns while it is off. Check it, and if it prints
  `0`, an administrator enables it once, then restarts pondNT's servers:

      (Get-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem).LongPathsEnabled
      Set-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem LongPathsEnabled 1

- **Disk and time for the source index.** About 6 GB for two label languages
  (21 GB with all ~190), on a data drive, not C:. The build takes 10-35
  minutes with the Defender exclusion, depending on the disk; many times
  longer without it (step 2).

## The short way: setup.ps1

After downloading and unpacking (step 1), in an elevated PowerShell window in the unpacked
folder, look first:

    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -WhatIf -Index J:\pondnt\d365.db

Build the index separately (step 5), then pass its existing path with
`-Index`. Setup checks bundled modules, selected configs/ports, index
readability and task conflicts; it does not probe shells, jobs, long paths,
d365fo.tools or SQL. Preview changes nothing and needs no elevation.
Then apply in the elevated window:

    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Index J:\pondnt\d365.db

It asks before each change; `-Yes` approves every question, including Defender.
Adding an exclusion still requires `-AddDefenderExclusion`:

- sets up the five default services on 127.0.0.1, each with its own access
  key and scheduled task (`-Services` chooses services, `-SkipServices`
  skips them);
- with the default selection, publishes `boring-pondnt`, `boring-knowledge`
  and `boring-gateway`. A custom selection publishes direct entries for the
  selected core and Knowledge servers, and adds Ferry when a specialist is
  selected. Ferry offers `service_list`, `service_describe`, `service_read` and
  `service_call`. Each specialist keeps its own task, port and key;
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
explains the trade-offs. Standard setup uses this fixed layout.

The gateway layout is fixed: core and Knowledge are direct; every other installed
service is behind Ferry. Setup generates ferry.json from its service plan and the
effective URLs, referencing each backend's own client.headers.json. Browser,
warehouse and data remain opt-in: prepare their settings, then add the service
with -Services. Existing owned tasks remain in the scope automatically.
There is no operator gateway roster.

Task actions invoke pythonw directly; `%` in a path is passed literally.

Without elevation it stops with "Run this in PowerShell as Administrator."
It never touches Defender unless you agree or add `-AddDefenderExclusion`
(step 2: dangerous), and never installs d365fo.tools (step 6).
[What setup installs](#what-setup-installs) lists every task and folder.
The steps below are what it does, by hand.

## 1. Download, check and unpack

Releases are on
[GitHub](https://github.com/the-boring-crocodile/boring-pondnt/releases);
candidates are marked as pre-releases. Each has the zip and a `.sha256` file.
Run the download and unpack block in an elevated PowerShell window for your
own account; writing under Program Files needs that elevation. `curl.exe`
comes with Windows. This block downloads pondNT 0.3.0. Read the
[0.3.0 release page](https://github.com/the-boring-crocodile/boring-pondnt/releases/tag/v0.3.0).
After unpacking, read **Upgrading** in `CHANGELOG.md` beside `setup.ps1` in the ZIP.

    $v = '0.3.0'; $tag = 'v0.3.0'
    $f = "boring-pondnt-$v-win-x64.zip"
    $u = "https://github.com/the-boring-crocodile/boring-pondnt/releases/download/$tag"
    cd "$env:USERPROFILE\Downloads"
    curl.exe -fLO "$u/$f"; curl.exe -fLO "$u/$f.sha256"
    if ((Get-FileHash $f -Algorithm SHA256).Hash -eq (Get-Content "$f.sha256").Split(' ')[0]) {
        Unblock-File $f
        Expand-Archive $f "C:\Program Files\pondNT" -ErrorAction Stop
        $folder = if ($v -match '^(\d+\.\d+\.\d+)rc(\d+)$') { "pondnt-$($Matches[1])-rc$($Matches[2])" } else { "boring-pondnt-$v-win-x64" }
        $p = "C:\Program Files\pondNT\$folder"
        if (-not (Test-Path -LiteralPath "$p\setup.ps1" -PathType Leaf)) { throw "Missing setup.ps1 under $p" }
        "unpacked: $p"
    } else { Write-Error "checksum does not match: delete $f and download again" }

It must end with `unpacked:` and the folder; read anything red above it. A
wrong checksum unpacks nothing; delete old copies of the two files from
Downloads first, so the check compares what you just downloaded. A browser may warn that the zip is not commonly
downloaded, or Defender may look at it closely: it holds Python,
PowerShell 7 and their programs. `Unblock-File` removes the Mark of the Web,
which every file unpacked from the zip would otherwise inherit.

The zip holds one folder; `$p` is that folder, and every command below uses
it. Everything runs from there: Python 3.14 and PowerShell 7 are inside,
nothing else is needed.

**Who may change that folder.** Install under `C:\Program Files\pondNT\<version>`
with permissions controlled by the host administrator. Core and D365 tasks run
elevated: anyone who can replace their Python or add a startup file there can
execute code with that elevation.

Setup inspects release owners and write grants read-only. It warns sternly
about potential write access, reparse points and unreadable paths, then
continues. It never changes ACLs or ownership, and makes no effective-access
or parent-directory security guarantee. Review the host's permissions yourself;
a warning is not a repair.

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

`pythonw.exe` runs the logon tasks without a window. With
`-AddDefenderExclusion`, setup asks about both interpreters (default no;
`-Yes` accepts it). `-Uninstall` offers to remove this release's exclusions;
`-Yes` accepts that removal too.

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

Setup creates each selected server's configuration once at
`%LOCALAPPDATA%\boring-pondnt\services\<server>\config.json`. After that the
file belongs to the operator. Setup reads it without rewriting it, except for
the one-time addition of `mode=keys` to the exact legacy local keys format. To change
an MCP port, edit both `port` and the port in its loopback `url`, then rerun
setup. Client entries, gateway upstream URLs and listener checks follow the
file. A duplicate port or unreadable config stops preparation before changes,
and the error names the file.

Canvas serves MCP and browser routes on the one configured listener (18511 by
default). Its resource URL ends in /mcp; removing that suffix gives the browser
prefix. For example, http://127.0.0.1:18611/drawing/mcp serves browser routes under
/drawing on port18611. The task action keeps only stable config/state paths;
setup and uninstall follow the URL's one port.

All normal services start as the person at logon through `pythonw.exe -X utf8`
and the in-process background logging bootstrap. Only core and the D365 servers
request Highest elevation. Knowledge, Canvas, Sourcecache and the gateway use
the person's ordinary token. Without autologon they start after desktop sign-in.

## 5. The source index (once per box)

    & "$p\boring-sourcecache-d365.cmd" build --source J:\AosService\PackagesLocalDirectory --db J:\pondnt\d365.db --name d365scm-10.0.49 --locales en-US,de

- `--source`: the box's PackagesLocalDirectory (`J:\AosService\...` on
  Azure dev boxes, `K:\AosService\...` on older ones -- look for it).
- `--db`: the index file to write, on a large data drive, not C:
  (`Get-PSDrive -PSProvider FileSystem` lists the drives and their free
  space). Its folder is created.
- `--name`: a label for this index, shown in every result and used to pick
  it when a server holds several, e.g. `d365scm-10.0.49` (product and
  version) or the environment's name. The index records the D365 version only
  when you give it (`--application-version`, `--platform-version`; see
  [Sourcecache](https://github.com/the-boring-crocodile/boring-services/blob/main/sourcecache-d365/README.md#building-a-snapshot));
  setup's own build gives none, so it says `unknown` there.
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
standard gateway layout, setup publishes these three entries
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
`service_list`, then `service_describe` for the service it needs.
Setup updates HTTP entries identified by their retained credentials. Customized
and unrelated entries stay in place; remove obsolete entries yourself.
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

The servers keep running between Visual Studio sessions (jobs survive a
Visual Studio restart); each logs to `service.log` in its folder under
`%LOCALAPPDATA%\boring-pondnt\services\` (`core`, `d365`, ...). The D365 server runs
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

    powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Uninstall

After asking, it stops and unregisters pondNT's scheduled tasks and removes
their `.mcp.json` entries; then it offers to remove the Defender exclusions
for this release's python. Sourcecache's index is read from its owned task. Tasks you
changed by hand are kept and reported.

Then delete by hand what it lists, as you want: the service data
(keys, notebook, canvases, logs) in `%LOCALAPPDATA%\boring-pondnt\services`,
including each `services\<name>\client.headers.json` that can hold an active
bearer, any `.mcp.json.bak-*` next to the client configuration (they may hold active
bearer keys), the handbook in `%LOCALAPPDATA%\boring-pondnt\handbook`, the index file and its folder,
state and logs in `%LOCALAPPDATA%\boring-pondnt` (unless `--state-dir` said
otherwise), and last the folder `$p`.

### Recovery after an interrupted setup

Setup stops on a failed step and names the failure; some services may already
have restarted. Inspect the tasks/listeners, resolve the cause, preview the same
service selection, then rerun. Setup creates operator configs once, retains keys,
re-registers tasks and writes generated files only when they differ. It keeps no
setup-attempt journal.

Each service's private `client.headers.json` owns its bearer. Setup stores that
file before accepting its hash in `keys.json`, so a rerun repairs an interrupted
first creation or client publication without issuing another key. New keys and
migrated annual keys have a fixed 100-year technical expiry. Migration matches
the existing bearer against its accepted fingerprint and retains it only while
it is unexpired. At or after expiry, setup refuses without changing files;
review and use `-RotateKeys` to replace it. Ordinary reruns do not extend expiry.

Use `-RotateKeys` only to replace credentials deliberately. Its preview names
the selected services; applying stops their tasks before replacing credentials.
Rotation publishes replacement client entries before restarting the selected
tasks. A failed restart can therefore leave the new client entries in place.
After an interrupted rotation, rerun **without** `-RotateKeys` to finish with
the retained bearer. Other subjects' and manually labelled keys remain.
Keep these private files under the person's profile, with access limited to
that person, Administrators and SYSTEM. Never copy their contents into logs.

## 10. Update, and back

An update is the new release's setup run over the old installation, with
the same choices. Keep both release folders and the recovery copy through
acceptance. To go back, restore the old configuration and credentials, then
use the old setup for a person/logon install or import the recorded task XML
for the legacy SYSTEM/cmd install. Keys, the notebook, canvases, the handbook
and the index stay where they are throughout.

**1. Unpack beside the old one.** Download, check and unpack as in step 1,
into its own folder (`C:\Program Files\pondNT\boring-pondnt-NEW-win-x64`, or
`C:\Program Files\pondNT\pondnt-X.Y.Z-rcN` for a candidate). Leave the old
folder in place for rollback. Read **Upgrading** in the ZIP's `CHANGELOG.md`
beside `setup.ps1` and the
[0.3.0 release page](https://github.com/the-boring-crocodile/boring-pondnt/releases/tag/v0.3.0).
Keep the matching recovery copy for data and configuration restored by step 5.

**2. Make and verify the recovery copy.** Use an elevated PowerShell as
the Windows user who installed pondNT. Review the installed tasks in Task
Scheduler. Pause client work and stop the old pondNT tasks. Wait for their
processes and listeners to exit before copying the data. Leave the task
registrations in place until the copy below passes. It creates a new private
folder under your profile. Set `$client` to the old client path if you used
a custom `-McpJson` value.

~~~powershell
$recovery = Join-Path $env:LOCALAPPDATA ('pondnt-recovery-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
& {
    $ErrorActionPreference = 'Stop'
    $state = Join-Path $env:LOCALAPPDATA 'boring-pondnt'
    $client = Join-Path $env:USERPROFILE '.mcp.json'
    $tasks = @(Get-ScheduledTask -TaskPath '\pondNT\' | Sort-Object TaskName)
    if (-not $tasks.Count) { throw 'No tasks to back up under \pondNT\.' }
    if ($tasks | Where-Object { $_.State -in 'Running', 'Queued' }) { throw 'Stop the pondNT tasks first.' }
    if (-not (Test-Path -LiteralPath $state -PathType Container) -or
        -not (Test-Path -LiteralPath $client -PathType Leaf)) { throw 'Check the old state and client paths.' }

    # Creation fails if the destination already exists. Seal it before copying data.
    New-Item -ItemType Directory -Path $recovery | Out-Null
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    icacls.exe $recovery /inheritance:r /grant:r "*$($me):(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not set private recovery permissions.' }

    $known = [ordered]@{
        'pondNT boring-sourcecache' = 'sourcecache'
        'pondNT boring-knowledge' = 'knowledge'
        'pondNT boring-canvas' = 'canvas'
        'pondNT boring-pondnt' = 'core'
        'pondNT boring-d365' = 'd365'
        'pondNT boring-d365whsapp' = 'd365whsapp'
        'pondNT boring-d365web' = 'd365web'
        'pondNT boring-d365data' = 'd365data'
    }
    $services = @($known.Keys | Where-Object { $_ -in $tasks.TaskName } | ForEach-Object { $known[$_] })
    if (-not $services.Count) { throw 'Review the task names and record the service selection.' }
    $index = $null
    $sourcecache = $tasks | Where-Object TaskName -eq 'pondNT boring-sourcecache'
    if ($sourcecache) {
        $arguments = @($sourcecache.Actions | ForEach-Object Arguments) -join ' '
        $match = [regex]::Match($arguments, '(?<!\S)"?--db"?\s+(?:"([^"]+)"|([^\s"]+))')
        if (-not $match.Success) { throw 'Review the Sourcecache action for its existing --db path.' }
        $index = if ($match.Groups[1].Success) { $match.Groups[1].Value } else { $match.Groups[2].Value }
        if (-not (Test-Path -LiteralPath $index -PathType Leaf)) { throw 'Check the existing Sourcecache index path.' }
    }

    $n = 0
    $records = @(
        foreach ($task in $tasks) {
            $xml = 'task-{0:D3}.xml' -f (++$n)
            Export-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath |
                Set-Content -LiteralPath (Join-Path $recovery $xml) -Encoding Unicode
            [pscustomobject]@{
                Name = $task.TaskName
                Path = $task.TaskPath
                Principal = $task.Principal | Select-Object UserId, GroupId, RunLevel
                LogonType = [string]$task.Principal.LogonType
                Action = @($task.Actions | Select-Object Execute, Arguments, WorkingDirectory)
                Xml = $xml
            }
        }
    )
    ConvertTo-Json -InputObject $records -Depth 6 |
        Set-Content -LiteralPath (Join-Path $recovery 'tasks.json') -Encoding UTF8
    @{ Services = $services -join ','; Index = $index } | ConvertTo-Json |
        Set-Content -LiteralPath (Join-Path $recovery 'selection.json') -Encoding UTF8
    Copy-Item -LiteralPath $state -Destination $recovery -Recurse -Force
    $clientCopy = Join-Path $recovery '.mcp.json'
    Copy-Item -LiteralPath $client -Destination $clientCopy

    $stateCopy = Join-Path $recovery 'boring-pondnt'
    $saved = Get-Content -LiteralPath (Join-Path $recovery 'tasks.json') -Raw | ConvertFrom-Json
    $selection = Get-Content -LiteralPath (Join-Path $recovery 'selection.json') -Raw | ConvertFrom-Json
    if (@($saved).Count -ne $tasks.Count -or
        @(Get-ChildItem -LiteralPath $recovery -Filter 'task-*.xml' -File).Count -ne $tasks.Count -or
        $selection.Services -ne ($services -join ',') -or $selection.Index -ne $index -or
        -not (Test-Path -LiteralPath $stateCopy -PathType Container)) { throw 'Recovery copy is incomplete.' }
    foreach ($record in $saved) {
        $definition = [xml](Get-Content -LiteralPath (Join-Path $recovery $record.Xml) -Raw)
        if (-not $definition.Task) { throw 'An exported task XML is incomplete.' }
    }
    if ((Get-FileHash -LiteralPath $client).Hash -ne (Get-FileHash -LiteralPath $clientCopy).Hash) {
        throw 'Client configuration copy differs.'
    }
    $serviceRoot = Join-Path $state 'services'
    foreach ($config in (Get-ChildItem -LiteralPath $serviceRoot -Directory -Force |
            Get-ChildItem -Filter '*.json' -File -Force)) {
        $copy = Join-Path $stateCopy $config.FullName.Substring($state.Length + 1)
        if ((Get-FileHash -LiteralPath $config.FullName).Hash -ne (Get-FileHash -LiteralPath $copy).Hash) {
            throw 'A service configuration copy differs.'
        }
    }
    "Verified recovery copy: $recovery"
}
~~~

Keep the folder private. The copy records every task, including any custom
tasks and the old gateway. Review `selection.json` against the installed
services before step 3. Leave the gateway out of `-Services` because setup
derives its new routing. Keep the existing Sourcecache index at its
recorded path. Customized tasks need manual review.

**Legacy SYSTEM/cmd layout (including published 0.2.10).** Plan an outage
for the upgrade. After the recovery copy passes and its tasks are stopped,
unregister the old SYSTEM/cmd tasks in Task Scheduler. Keep the
person/logon registrations and existing client settings. The preview in
step 3 comes **after** stopping the old tasks and unregistering the SYSTEM
tasks. If you need to go back from this point, use the recovery copy and
the matching step 5 below.

**3. Look, then apply**, in an elevated PowerShell in the new folder.

- **Person/logon layout throughout.** Omit `-Services` and `-Index` to update
  the current owned task scope when it matches the recorded selection. Supply
  `-Services` if setup reports configured services outside that scope, or to add
  a prepared service. Existing owned services remain included.

      powershell -ExecutionPolicy Bypass -File .\setup.ps1 -WhatIf
      powershell -ExecutionPolicy Bypass -File .\setup.ps1

- **Legacy SYSTEM/cmd layout.** Pass the complete service selection and,
  when selected, Sourcecache's existing index recorded in step 2. Use the
  same PowerShell window or set `$recovery` to your verified folder first.

      $selection = Get-Content -LiteralPath (Join-Path $recovery 'selection.json') -Raw | ConvertFrom-Json
      $legacy = @('-Services', $selection.Services)
      if ($selection.Index) { $legacy += @('-Index', $selection.Index) }
      powershell -ExecutionPolicy Bypass -File .\setup.ps1 @legacy -WhatIf
      powershell -ExecutionPolicy Bypass -File .\setup.ps1 @legacy

  Standard setup builds the fixed gateway from that service selection.
  Old direct routes and gateway choices do not determine the new layout.

The preview should say "key kept" for every existing service and name every
service you recorded in step 2. It lists service folders outside the current
run and refuses to proceed without `-Services` if it finds any. Check the
scope before applying. For an old local config, the preview lists
`config.json: adds mode=keys` under its service. Apply adds that field before
switching tasks. Use `-RotateKeys` only for deliberate replacement.
`-Yes` alone adds no Defender exclusion; `-Yes -AddDefenderExclusion` adds it
without another question. Each release's Python needs its own exclusion if you
choose one (step 2).

**4. Check.** Every selected task now starts the new folder's python (inspect the scheduled task actions), `.\boring-pondnt-core.cmd --self-check` passes,
and Visual Studio's `bootstrap` names the expected server/version. With
Ferry, inspect `service_list` and make one harmless read against each backend
you need. A running task or listening gateway does not establish backend
health. Keep the developer logged on: core and D365 backends are interactive
logon tasks, not unattended boot services.

If apply stops, inspect the named failure and actual tasks/listeners, then
[rerun setup without -RotateKeys](#recovery-after-an-interrupted-setup) after
resolving the cause. A partial change is not an instruction to uninstall.

**5. Back, if needed.** The old release cannot read a local keys config with
the added `mode` field. Before starting its tasks, restore the step 2 snapshot,
including each `services\<name>\client.headers.json` and the matching keys
the old release accepts. Follow the branch recorded in step 2:

- **Person/logon layout:** stop the new tasks with the new release's uninstall,
  then restore the matching old config, client settings and credential snapshot.
  Follow the old release's setup procedure with the saved task scope and index;
  older setup versions cannot read every new format or derive those arguments.
- **Legacy SYSTEM/cmd layout:** from the new folder, run its
  `powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Uninstall`.
  This reads the new owned task scope, stops/removes those tasks and keeps data.
  Restore the matching old configs, client settings and credentials from
  the recovery copy. Import the exported task XML in Task Scheduler under
  the recorded task paths/names and original principals. Start those tasks.

For either branch, check the restored old release. Compare task executable
paths and triggers with the step 2 record, confirm the restored client
entries in `.mcp.json`, and make one authenticated read through the restored
client.

**6. Tidy up only after accepting the forward upgrade.** Remove the Defender
exclusions for the old Python executables. If the old release lived under
`C:\tools`, for example, use
`Remove-MpPreference -ExclusionProcess 'C:\tools\boring-pondnt-OLD-win-x64\python\python.exe'`
and the same for `pythonw.exe`. Then delete the old release folder from an
elevated prompt when its Program Files permissions require it. Do not run the old
release's `-Uninstall` for this: it removes the tasks, which now belong to
the new release.

**The separate tunnel.** The optional tunnel has its own window and state,
independent of setup.
Disable old tunnel logon/ensure tasks and stop their old processes before
opening it. Keep their private records until migration is accepted; the new
launcher leaves them in place. Follow the [tunnel migration](DEVBOX_TUNNEL.md#move-from-the-previous-tunnel)
for the Windows handoff and Linux component. Run setup from the desktop.

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

The standard base setup has six scheduled tasks: five services plus
Ferry. Selecting all three browser/WHS/data opt-ins makes nine.
Tasks live under `\pondNT\`, each starting one server on
127.0.0.1. Each restarts up to three times, a minute apart, and has no time
limit. `%LOCALAPPDATA%` is that of the person who ran setup.

| Task | Server, port | Runs as | Starts | Elevated | Its folder |
|---|---|---|---|---|---|
| `pondNT boring-pondnt` | core, 18513 | you, through `pythonw.exe` (no window) | at logon | yes (highest run level) | `%LOCALAPPDATA%\boring-pondnt\services\core` |
| `pondNT boring-d365` | D365 server, 18514 | you, through `pythonw.exe` | at logon | yes | `...\services\d365` |
| `pondNT boring-d365web` | D365 browser, 18516 (opt-in) | you, through `pythonw.exe` | at logon | yes | `...\services\d365web` |
| `pondNT boring-d365whsapp` | WHS app/form, 18515 (opt-in) | you, through `pythonw.exe` | at logon | yes | `...\services\d365whsapp` |
| `pondNT boring-d365data` | OData/DMF, 18518 (opt-in) | you, through `pythonw.exe` | at logon | yes | `...\services\d365data` |
| `pondNT boring-sourcecache` | Sourcecache, 18501 | you, through `pythonw.exe` | at logon | no | `...\services\sourcecache`; the index file where `-Index` says |
| `pondNT boring-knowledge` | Knowledge, 18512 | you, through `pythonw.exe` | at logon | no | `...\services\knowledge` (`notebook.db`) |
| `pondNT boring-canvas` | Canvas MCP and browser, 18511 | you, through `pythonw.exe` | at logon | no | `...\services\canvas` |
| `pondNT boring-gateway` | the gateway (boring-mcp-ferry), 18517; when a selected backend uses it | you, through `pythonw.exe` | at logon | no | `...\services\gateway` (generated `ferry.json`; references each backend's `client.headers.json`) |

Each folder holds the server's `config.json`, `client.headers.json`, accepted
key file (normally `keys.json`) and `service.log`. Generated Ferry routes follow the owned task scope and reference those header files.
Besides the tasks, setup writes:

- `%USERPROFILE%\.mcp.json`: entries for the selected core and Knowledge
  servers and the generated gateway, when present. A needed change gets a
  backup next to it (`.mcp.json.bak-<time>-<id>`); backups can contain active
  bearer keys. Keep them private. An unchanged file is left alone;
- `%LOCALAPPDATA%\boring-pondnt\handbook`: the handbook, copied once;
- Defender process exclusions for this release's `python.exe` and
  `pythonw.exe`, only with `-AddDefenderExclusion` and approval.

The core keeps its job output and call log in `%LOCALAPPDATA%\boring-pondnt`.
The optional Pond tunnel defaults to 18530/18532 for core/D365 and
18534/18536 for the warehouse/browser servers, plus 18538 for d365data.

`.\setup.ps1 -Uninstall` removes the `.mcp.json` entries and the tasks it
recognises as its own, and offers to remove the Defender exclusions. It keeps
the folders, the handbook and the index, and lists them for you (step 9).
Remove `services\<name>\client.headers.json` separately when discarding service
credentials. Those bearer keys may still be accepted by the retained key
files. Client backups can contain the same keys.

### Why these accounts

Every normal service runs as the person at logon and shows no console window.
Core and the D365 servers use Highest elevation because their commands and
D365 tooling need that authority. The other services use the person's ordinary
token. No service starts before desktop sign-in; autologon is the operator's
choice, not something setup configures.

This installer is limited to the single-administrator disposable development
box. Establish administrator-controlled permissions for the release folder
and review the permission warning in step 1. The state directory belongs
to the developer, who is an administrator. Shared-machine service identities
are outside this install.

### Recognising its own tasks

Setup recognizes existing tasks by their complete service command, working
directory, principal and trigger. A matching name alone is not
ownership. Customized or unrecognized definitions are retained and require
manual review; automatic upgrade refuses them before writing service settings.
Uninstall only considers owned service entries. Sourcecache's index comes from its verified task, including during uninstall. Setup stops only the checked Scheduler task, then
waits for that task to stop and its configured ports to clear. It never kills
an arbitrary listener or reconstructs ownership of leftover processes. Startup
checks the task is Running and all configured listeners exist; this does not
prove authenticated tool behavior, which the acceptance steps check separately.

### Access keys

Every server lets in only callers it knows. On this machine that is an
**access key** per server. Setup makes one key per server for you (role
writer, fixed 100-year technical expiry). Setup keeps the bearer in the service's
private `client.headers.json` and its accepted hash in the configured key file.
A direct client entry copies that header; Ferry references the same file.
Ordinary reruns retain the key and expiry. `-RotateKeys` explicitly replaces
the selected setup keys. The servers listen on this machine's loopback
interface only, but the key itself is not bound to the device: it is a bearer
secret, and whoever holds it and can reach the port acts with its role. Keep
`.mcp.json` private.

**Keys by hand** (for example for a colleague, or an offsite service over HTTPS). Not for
the core: its `file_read` reads `.mcp.json` too, so every core key is a full key:

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
effect at the next call, no restart. Use the client's documented secret-entry
mechanism when it supports one;
input-variable syntax is client-specific. [Other clients](CLIENTS.md)
distinguishes the VS Code format from Visual Studio and other harnesses.

### The handbook

The core serves `%LOCALAPPDATA%\boring-pondnt\handbook`.
The first setup copies the release's `handbook\` there. Later releases leave
it alone and report new pages separately from files that differ. Ferry uses
the release's copy when a service guide is missing from your handbook.
To adapt a missing guide, copy it into your handbook, edit it and rerun
setup so Ferry uses your copy. Bootstrap names the release copy
`release_handbook`. To take the full release handbook as it is, rename or
remove your handbook folder and run setup again. `-Uninstall` keeps it.

### The general services on their own

Sourcecache, Knowledge and Canvas are separate applications. Each frontend
uses one authentication mode: `keys` (local access keys), `crocodile`
(Crocodile-issued tokens), or `issuer` (its own OAuth issuer).
A single frontend cannot combine keys and OAuth. The release folder has
their configuration guide
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
