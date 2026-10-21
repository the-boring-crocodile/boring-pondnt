# boring-pondnt-d365 — operations and compiled references

The do side of D365 development for Copilot: build, see the compiler's
results, sync the database, run best practice, start and stop the
environment, and know the environment's state. A thin, typed layer over
d365fo.tools. Four read-only xref tools answer compiled references;
source text and labels live in the independent boring-sourcecache-d365.

Measured on a D365 development box (d365fo.tools 0.7.34):
`Import-Module d365fo.tools` takes **18.3 s** in Windows PowerShell 5.1; the
module has 227 commands.

## D365 tools (nine; plus four xref tools)

| Tool | d365fo.tools | Kind | Result |
|---|---|---|---|
| `d365_status` | `Get-D365Environment -All`, `Get-D365Url`, module and application versions, elevation | read | services (name, status, start type), URL, versions, PackagesLocalDirectory, whether the server runs elevated |
| `d365_models` | `Get-D365Model` (`-Name`, `-ExcludeMicrosoftModels`, `-CustomizableOnly`) | read | name, module, layer, customizable -- live, not the index's snapshot |
| `d365_build` | `Invoke-D365ModuleBuild -Module` (`-IncludeReports`) | job | job id, log folder |
| `d365_results` | the diagnostics files of xppc (`Dynamics.AX.<Module>.xppc.xml`) and xppbp (`...xppbp.xml`) -- one format, what Visual Studio reads (`Get-D365CompilerResult` only counts, the analyzer writes Excel) | read | `kind` compile or best_practice (per module): totals by severity, then errors (and warnings) as rows: severity, type, element type, path, line, column, message, item -- bounded; `kind` sync: SyncEngine's own logs as mode, steps with times, total, errors as rows (SyncEngine runs with ContinueOnError: exit 0 can hide errors) |
| `d365_db_sync` | `Invoke-D365DbSyncModule -Module` or `Invoke-D365DbSync` (full), `-LogPath` into our state folder | job | job id; refused while a sync runs; results through `d365_results kind=sync` |
| `d365_best_practice` | `Invoke-D365BestPractice -Module -Model -LogPath` | job | job id; results through `d365_results kind=best_practice` |
| `d365_environment` | `Start-` / `Stop-` / `Restart-D365Environment` (`-All` or `-Aos`, `-Batch`, ...) | act | services before and after |
| `d365_job` | the service's own jobs | read/stop (write annotation) | state, exit code, duration, last lines of output; `wait_seconds` 0–60 observes the same job; `stop=true` requests termination |
| `d365_sql` | pyodbc, Windows authentication (not d365fo.tools: `Invoke-D365SqlScript` returns no rows) | act | per batch (`GO`): result sets as columns and rows (bounded), rows changed, messages; errors and timeouts as failures naming the batch; `atomic` = one transaction |

`d365_job` without an id lists jobs. With an id, `wait_seconds` defaults to 0;
`wait_expired=true` means execution or cleanup has not settled; it is not
permission to restart the command. Stop and a nonzero wait cannot be combined. Because the same tool
can stop work, its MCP annotation is write-capable even for ordinary status calls.
The tool waits for tree cleanup before publishing success; unconfirmed
cleanup reports failed.

Build, sync, best practice and environment actions support `dry_run`: d365fo.tools'
`-OutputCommandOnly` -- the exact command, nothing run. Copilot can show it
before asking; we can test on a box without changing it.

Left out on purpose (add later only from the call log): running classes
(`Invoke-D365SysRunnerClass` -- runs code in a browser session), users, LCS,
bacpac, deployable package install, model import/remove. Free-form PowerShell
stays the core's `exec`.

## Observation and ownership

These tools do not authorize managing Visual Studio-owned work outside VS.
The kit's [VS workflow](../kit/visualstudio.md) owns that boundary; standalone
operations need their own agreed scope and must not compete with IDE work.

A stopped IIS service does not establish application unavailability: Visual
Studio may host IIS Express. Confirm the hosting owner before service changes.

For compile/BP results, `d365_results` compares compatible files from its own
log directory and the installed package's diagnostics (which may come from VS).
It returns the latest modification time across those locations, the chosen
`file`, `written` time and `log_source` (`pondnt` or `package`).
This is not a source-revision or job receipt. Match the path, time and scope
to the intended build/check; a newer file can still belong to unfinished work.
If package lookup fails (including startup/loading or a failed PowerShell
session), existing pondNT logs remain readable. The result adds
`package_lookup_error` (the error code) and a note that only pondNT logs were
checked; a newer package diagnostic may exist. Raw lookup error output is not
included in this fallback. With no pondNT log, the original lookup error is
returned. A malformed/unreadable retained log remains an error.

The normal path still queries the package location to compare freshness; it
does not cache that location or skip discovery merely because a local log exists.
Investigate incomplete evidence rather than accepting an unrelated success.
A missing diagnostic file does not require a new build.

## Shape

- **One long-lived Windows PowerShell 5.1** inside the server, with
  `Import-Module d365fo.tools` and `Enable-D365Exception` done once at start
  (18 s, in the background; `d365_status` says "loading" until then). Read
  tools run there, one at a time, results as compact JSON
  (`ConvertTo-Json -Compress`).
- **Jobs in their own PowerShell process** (build, sync, best practice can
  take minutes to hours): the service's own small job handling (its own, not
  the core's), output to a log file, `d365_job` to look.
- **Elevation:** many cmdlets need an elevated session. The server does not
  inherit it from Visual Studio: setup.ps1 runs it as its own scheduled task,
  as you at logon with the highest run level, so it is elevated whether or
  not Visual Studio is. `d365_status` says whether it is; started by hand
  without elevation, the cmdlets that need it fail.
- **Missing module:** PowerShell-dependent tools refuse with `not_found` and "ask the user to
  install d365fo.tools"; nothing installs on its own.
- **Same contract as the others:** HTTP only -- on loopback with a key
  for Visual Studio, as a Pond service through the tunnel -- compact JSON,
  outcomes, call log, short argument errors.

## Testing

- On Linux: the server with a fake PowerShell (the protocol, parsing,
  bounds, refusals, jobs).
- On the box, without changing it: `d365_status`, `d365_models`,
  `d365_results` on an existing log, every acting tool with
  `dry_run`.
- On the box, changing it (the operator's go each first time): one build of a
  small model, one module sync, one restart.

## Open

- Which model to build first on the dev box (small, standard or ours).
- Whether Visual Studio's call timeout matters for `d365_status` (one
  PowerShell round trip, ~1 s once loaded).

## Compiled references and dependencies

The four existing `xref_info`, `xref_names`, `xref_references` and
`xref_hierarchy` tools now register in this server, once, with unchanged
schemas, bounds and read-only annotations. [XREF_SERVICE.md](XREF_SERVICE.md)
retains the feature reference and original live measurements. All 13 tools
use the same result/log conventions; xref retains its separate query module,
parameterized fixed SELECTs, binary ordering and connection cleanup.

| Capability | Dependency | Failure scope |
| --- | --- | --- |
| PowerShell operations | Windows PowerShell 5.1 + d365fo.tools | xref and SQL remain available |
| AxDB SQL | pyodbc / Windows SQL authentication | PowerShell and xref remain independent |
| Compiled references | pyodbc / DYNAMICSXREFDB | D365 operations remain available |
| Discovery | none of these external dependencies | always lists all 13 tools |

The PowerShell session starts in the background and serializes its own calls.
The pinned MCP 2.2 SDK dispatches synchronous tools on worker threads; xref
queries do not wait for the PowerShell session lock. SQL access is lazy.

## Startup and SQL configuration

    boring-pondnt-d365.cmd serve
    boring-pondnt-d365.cmd serve --sql-server localhost --xref-server sql2 --xref-database DYNAMICSXREFDB --xref-driver "ODBC Driver 17 for SQL Server" --xref-stall-seconds 120

`--sql-server` defaults to localhost and supplies xref's default instance.
`--xref-server` overrides only xref. `--xref-database` defaults to DYNAMICSXREFDB;
`--xref-driver` selects a driver (otherwise newest installed SQL Server ODBC,
then Windows SQL Server). `--xref-stall-seconds` defaults to 120; 0 means never.
AxDB remains `d365_sql`'s default database, independently selected per call.

`serve --config config.json` is the only way to start it: authenticated HTTP (boring-service-kit's ServiceConfig): a Crocodile's
introspection and registration with its `access` list as the allowed Ponds
(what tools/devbox-start.ps1 writes), the server's own issuer, or access keys.
setup.ps1 writes the loopback configuration (access keys, 127.0.0.1:18514) and
runs it at logon as the person, elevated. All 13 tools in every mode. These Windows-only exceptions do
not change the general services' HTTPS/OAuth target.

## The retired read-only xref server

There used to be a separate, read-only xref server. There isn't anymore: the
four xref tools live in this server now, next to tools that can build, sync
and run SQL. Setup does not quietly reuse an old `d365-xref` entry -- it
reports it for removal by hand, because an identity granted for reading
references should not inherit the power to restart an environment without
someone looking at that decision first.

## Session output bounds

SessionOutput owns the finite protocol buffer: at most 16 queued lines and
1 MiB of input bytes per line, including any newline. The reader uses bounded
readline even for an unterminated line. Buffer overflow fails closed rather than
dropping a reply; this deliberately also refuses bursts that outrun the consumer.
The bound is on input bytes/frames, not an exact Python heap budget.

Normal diagnostic noise retains only the last 20 lines, each clipped to 4096
characters. Explicit host-output calls preserve their lines up to 4 MiB of
characters (including one separator per line); overflow fails rather than
returning a silently incomplete dry-run command. JSON syntax, object shape and
the boolean ok field are checked for both ready and result frames. Errors carry
fixed reasons, not raw output. Known idle output failure refuses before dispatch.

An output/protocol failure marks the session failed and requests root
termination; it does not automatically restart or replay the command. Ordinary
timeout restart behavior is unchanged.

## Containment and shutdown

boring-service-kit.process owns Tree and the shared wait/stop/timeout/cleanup
settlement algorithm; core exec/jobs and D365 consume that owner directly.
Service independence is a rule, but not a reason to duplicate Windows
containment. The kit's optional process extra declares pywin32==311 on
Windows; pondNT opts in. Unrelated service consumers need not import or
activate process handling.

D365 Session uses the shared suspended-start Job Object with piped stdin.
Startup/publication, teardown and admission are serialized; a closed session
cannot restart. Boot generations cannot publish stale readiness. Teardown
confirms the tree, joins the output reader and closes its pipe endpoints.
Failed cleanup retains ownership, reports cleanup_confirmed=false, and blocks
restart. Ordinary call timeout restarts only after confirmed old-tree cleanup.

D365 Jobs owns one watcher per job and uses the shared settlement algorithm.
States are running, stopping, exited, stopped or failed. Status does not perform
cleanup. wait_expired includes pending cleanup. A failed close can never report
stopped. Shutdown closes admission before waiting for existing owners; it reports
unconfirmed cleanup as a failure. Natural root completion also closes descendants.
Sync admission plus old-log preparation are atomic under the job admission lock;
stopping and failed-unconfirmed syncs block successors and preserve existing logs.

MCP lifespan owns normal shutdown in both transports. SDK 2.2's HTTP lifespan
was inspected: it runs once per server manager, not per client connection.
The CLI finally path also closes resources if transport setup fails before the
lifespan starts. Startup occurs after server/tool construction is complete.
