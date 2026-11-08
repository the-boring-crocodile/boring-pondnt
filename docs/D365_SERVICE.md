# boring-pondnt-d365 — operations and compiled references

Build a D365 model, inspect compiler diagnostics, sync its database and run
best-practice checks from an agent. The service keeps d365fo.tools loaded in
one PowerShell session so each command does not pay the import cost again.
Build, sync and best-practice operations get retained jobs that the agent can
observe after the original call returns. Use `background=true` for the same
job lifecycle with a long `d365_run` command.

The MCP connection is **authenticated HTTP only; stdio is not supported**.
The recommended [pondNT setup](INSTALL.md#the-short-way-setupps1) keeps core
and Knowledge direct and carries this backend through Ferry. The backend
normally listens on loopback port 18514; Copilot calls it through Ferry on
18517. Starting it from a terminal still starts an HTTP server.

The same server offers read-only compiled references through `xref`.
Use [Sourcecache](https://github.com/the-boring-crocodile/boring-services/blob/main/sourcecache-d365/README.md)
for source text and labels. Neither a source snapshot nor an old compiler log
establishes that the current edits build: match evidence to the intended work.

Start with [Observation and ownership](#observation-and-ownership), then
[Startup and SQL configuration](#startup-and-sql-configuration) or the
[tool reference](#d365-tools). For a pending or failed command,
[read the result before retrying](#read-the-result-before-retrying).

## D365 tools

| Tool | d365fo.tools | Kind | Result |
|---|---|---|---|
| `d365_run` | any d365fo.tools command or short script, in the warm session (`& { ... } \| Out-String -Width 220`) | act | the console text with host output, labelled credentials masked before it is cut (`max_chars`, default 20,000); foreground `timeout` 1-600 s (the Pond's bound behind a Pond), default the smaller of 60 s and that bound; `background=true`: a job (`d365_job`), ignoring foreground `timeout` |
| `d365_build` | `Invoke-D365ModuleBuild -Module` (`-IncludeReports`) | job | job id, log folder |
| `d365_results` | the diagnostics files of xppc (`Dynamics.AX.<Module>.xppc.xml`) and xppbp (`...xppbp.xml`) -- one format, what Visual Studio reads (`Get-D365CompilerResult` only counts, the analyzer writes Excel) | read | `kind` compile or best_practice (per module): totals by severity, then errors (and warnings) as rows: severity, type, element type, path, line, column, message, item -- bounded; `kind` sync: SyncEngine's own logs as mode, steps with times, total, errors as rows (SyncEngine runs with ContinueOnError: exit 0 can hide errors) |
| `d365_db_sync` | `Invoke-D365DbSyncModule -Module` or `Invoke-D365DbSync` (full), `-LogPath` into our state folder | job | job id; refused while a sync runs; results through `d365_results kind=sync` |
| `d365_best_practice` | `Invoke-D365BestPractice -Module -Model -LogPath` | job | job id; results through `d365_results kind=best_practice` |
| `d365_job` | the service's own jobs | read (reader); `stop` needs writer (write annotation) | state, exit code, duration, last lines of output; `wait_seconds` 0–60 observes the same job; `stop=true` requests termination |
| `d365_logs` | fixed AxDB reads and classic Windows logs | read | severity totals and recent rows, optionally grouped by repeat |
| `d365_sql` | pyodbc, Windows authentication (not d365fo.tools: `Invoke-D365SqlScript` returns no rows) | act | per batch (`GO`): result sets as columns and rows (bounded), rows changed, messages; errors and timeouts as failures naming the batch; `atomic` = one transaction |

`d365_job` without an id lists jobs. With an id, `wait_seconds` defaults to 0;
`wait_expired=true` means execution or cleanup has not settled; it is not
permission to restart the command. Stop and a nonzero wait cannot be combined. Because the same tool
can stop work, its MCP annotation is write-capable even for ordinary status calls.
The job owner attempts tree cleanup before publishing a terminal outcome;
unconfirmed cleanup reports failed. An exited job can have a nonzero exit code,
and even exit0 does not establish successful D365 processing: inspect the
matching diagnostics.

`d365_run.timeout` bounds only the foreground session call. With `background=true`,
the request still validates argument types, command and output bounds, but does not
apply the foreground timeout range. The existing D365 job owner runs the command
until it exits, is stopped through `d365_job`, or the server closes; `timeout` does
not set a job deadline. A local Ferry alone does not configure a Pond call budget.

Build, sync and best practice support `dry_run`: d365fo.tools'
`-OutputCommandOnly` -- the exact command, nothing run. Copilot can show it
before asking; we can test on a box without changing it.

There are no dedicated wrappers for running classes, managing users, LCS,
bacpac, deployable packages or model import/removal. This is an interface
choice, not a permission boundary: `d365_run` can execute other commands in
the service account's PowerShell session. Choose and authorize their scope deliberately. General
shell work outside that session belongs to the core's `exec`.

## Operational logs

`d365_logs(source, since?, level?, contains?, group?, limit?, max_chars?, until?, job?, status?, until_skip?)`
reads one source per call. It requires the reader role and has a read-only
annotation. Results, including unavailable sources, identify the service as
`server: {"name": "d365", "version": "0.2.10"}`. AxDB sources use the d365
server's configured Windows SQL identity and can include messages from multiple
companies.

| Source | Reads | Groups by |
|---|---|---|
| `batch` | Batch task history with job and task captions, IDs, status, company, executor and decoded INFO | Normalised task caption and first error text. Without an error message, uses the first decoded message or task status. |
| `process` | Process automation messages, linked to the scheduled occurrence or series name when available | Normalised message text |
| `events` | Classic box Application and System logs from WAS, .NET Runtime, IIS-W3SVC-WP, Dynamics* and Management Reporter*, or messages naming AOSService, Batch.exe or w3wp. Application Error requires a D365 faulting application or module name/path. | Provider, event ID and normalised first message line |
| `iis` | Classic box W3C requests for the configured AOSService site. Includes the path without the query string, status, substatus and elapsed milliseconds. | Method, normalised path and HTTP status, with maximum and mean elapsed milliseconds |

With `level=error` or `level=warning`, the scan narrows at the source. Batch
reads failed tasks or tasks with an infolog, process reads non-info messages,
and Windows reads the requested event levels. Totals cover scanned candidates,
so counts for lower levels can be incomplete. On the test box this turned 4,879 batch
tasks a day into 128 candidates.

The 10.0.49 source index has no standard data entity over `BatchHistory` or
`ProcessExecutionMessageLog`. Those sources use fixed parameterized SELECTs
on AxDB. The adapter does not use the exception log `SysExceptionTable`.
The probe found only demo rows, last written in 2018.

Read the database log with d365data, `odata_query(entity="DatabaseLogs", …)`.

- `since` defaults to 60 minutes ago and `until` defaults to now. Both accept
  1–43,200 minutes ago or an ISO timestamp within the last 30 days. A timestamp
  without an offset is UTC. All four sources read `since ≤ time < until`
  (`≤ until` with `until_skip`), newest first. `until` must be later than `since`. Use fixed ISO bounds for comparisons
  across calls, since minute offsets move relative to each call.
- `level` defaults to `error`. `warning` includes warnings and errors.
  `all` includes info and unclassified entries. In `batch`, error matches
  tasks with status `Error` or error entries in their infolog. A `Finished`
  task can carry errors. IIS uses 5xx and above for errors and 4xx for warnings.
- `status` filters batch tasks by a case-insensitive status name in SQL before
  the scan cap. Use `status="error"` for failed tasks only. Other sources refuse
  `status`. Batch rows show `status` at the top level as well as in `context`.
  A group shows the newest task's status and adds `status_counts` when it
  contains more than one status.
- `job` takes a case-insensitive literal substring of up to 200 characters. It
  matches the job or task caption in `batch` and the process name in `process`.
  SQL applies the parameterised match before the scan cap and escapes LIKE
  wildcards. `events` and `iis` refuse `job`.
- `contains` matches message text, batch captions, process names or IIS paths
  case-insensitively after reading the source. Totals count individual scanned
  matches before the final level filter, grouping and display cuts.
- Grouping defaults on. A grouped row shows the newest matching context,
  `first`, `last`, `count` and `distinct`, which counts different raw texts or
  IIS paths in that group. `count` counts scanned log records or tasks, so it can
  include repeated reports of the same business failure. Use `group=false` for individual rows.
- Text grouping replaces ISO-like date-times with `<time>`, dashed or compact
  GUIDs with `<guid>`, and runs of at least five digits with `<n>`. Date-times
  have a `YYYY-MM-DD` date, a `T` or space, and an `HH:MM:SS` time with optional
  fractional seconds and `Z` or `±HH:MM` offset. Explicit `process id` values
  also become `<n>` even when shorter, including the four-digit IDs in WAS
  events. Batch, process and event rows retain the newest text and context,
  subject to the usual credential masking.
- For grouped IIS rows, each balanced entity key predicate under `/data/`
  becomes `(…)`, including nested keys and URL-encoded predicates. Unkeyed
  segments and qualified action names keep their original spelling. The newest
  query-free raw path appears in `context.example_path`, subject to credential
  masking. Ungrouped rows show the original query-free path.
- `limit` defaults to 50, maximum 500. `max_chars` bounds the compact
  JSON row array at 32,768 characters by default, maximum 262,144.
  Notes distinguish display cuts from scan cuts that can make totals
  incomplete. A display cut suggests a larger `limit` or `max_chars`, or a
  narrower window or scan filter (`level`, `job`, `status`); `contains` is
  applied after the scan and does not make room in it.
- A scan cut that reaches a timestamp gives a cursor, `until=T` and
  `until_skip=n`, for the rows not yet scanned. With `until_skip`, `until` is
  inclusive and the first `n` rows stamped exactly `T` in the source's
  newest-first order, which earlier pages scanned, are skipped. Rows tied at
  the cut are therefore neither lost nor repeated, even when more rows share
  one second than fit in a page. `until_skip` requires the ISO `until` from the
  note. Keep the same `since` and filters: the count refers to rows the scan
  read before `contains`. AxDB orders ties by `RECID`; IIS by file and line,
  newest first. Windows channels are cut separately and have no stable order
  among equal times, so an events page leaves rows tied at the cut to the next
  page (`until_skip=0`). A Windows continuation can overlap older rows already
  seen from another channel.
  Labelled credentials are masked before display cuts.
- AxDB scans at most 1,000 rows. Batch INFO is limited to 1 MiB per
  task, 32 MiB per scan, depth 16 and 1,000 decoded entries per task.
  `undecoded` counts bytes left at a parse fault or skipped by the per-task
  bound. It can be zero when a closing marker is missing.
- Events scan up to 1,000 records per channel and have a text budget. If
  Windows supplies no formatted message, the same limits and masking apply
  to the joined insertion strings, marked by
  `context.text_source="insertion_strings"`. If both are empty, the row says
  so and marks `context.text_source="unavailable"`.
- IIS scans up to 64 files, 16 MiB and 20,000 rows. A file over the byte
  budget is read from its end. Rows before a W3C field header in that tail
  use the last complete `#Fields` header in the file's first 64 KiB (IIS
  repeats the header after a restart). Without one, those rows are reported
  as unreadable rather than guessed. A continuation re-reads the same end of
  such a file, so its older rows stay out of reach of this tool. Absent
  box log sources report `available: false` with a reason.

IIS buffers access logs, so the newest requests may be missing until IIS writes
them or an administrator runs `netsh http flush logbuffer`.
[Windows HTTP commands](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/netsh-http)

Batch status names come from the 10.0.49 `BatchStatus.xml` in sourcecache
snapshot `5ff33b1f`. The same snapshot references the kernel `Exception`
enum, whose names and values are published by Microsoft. The INFO decoder
uses the sample's `0xfb` tag as a four-byte little-endian length followed
by opaque data. At byte 213 in the large sample, 2,217 payload bytes lead
exactly to the three closing markers.
[Exception enum](https://learn.microsoft.com/en-us/dotnet/api/dynamics.ax.application.exception?view=dyn-finops-dotnet)
· [IIS W3C logging](https://learn.microsoft.com/en-us/iis/manage/provisioning-and-managing-iis/configure-logging-in-iis)

## Observation and ownership

A build, sync or service restart can compete with work already owned by Visual
Studio. Establish the current owner before starting another operation; two
ways to invoke a tool do not create two independent development environments.

These tools do not authorize managing Visual Studio-owned work outside VS.
The handbook's [VS workflow](../handbook/visualstudio.md) owns that boundary; standalone
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

## How it runs

- **One long-lived Windows PowerShell 5.1** inside the server imports
  d365fo.tools and enables exceptions once, in the background. Session calls
  are refused `locked` while it loads; elapsed time alone is not a
  readiness signal. `d365_run` and package discovery share that session,
  one call at a time. SQL and xref have separate connections.
- **Environment commands:** use `Get-D365Environment` or `Get-D365Model` through
  `d365_run`. Environment start/stop/restart also runs there, with
  `background=true` for a long operation. The former `d365_status`,
  `d365_models` and `d365_environment` wrappers are retired.
- **Jobs in their own PowerShell process** (build, sync, best practice can
  take minutes to hours): the service's own small job handling (its own, not
  the core's), output to a log file, `d365_job` to look.
- **Elevation:** many cmdlets need an elevated session. The server does not
  inherit it from Visual Studio: setup.ps1 runs it as its own scheduled task,
  as you at logon with the highest run level, so it is elevated whether or
  not Visual Studio is (`d365_run` with a `WindowsPrincipal` check says whether it is); started by hand
  without elevation, the cmdlets that need it fail.
- **Missing module:** PowerShell-dependent tools refuse with `not_found` and "ask the user to
  install d365fo.tools"; nothing installs on its own.
- **Same contract as the others:** HTTP only -- on loopback with a key
  for Visual Studio, as a Pond service through the tunnel -- compact JSON,
  outcomes, call log, short argument errors.

## Testing

- On Linux: the server with a fake PowerShell (the protocol, parsing,
  bounds, refusals, jobs).
- On the box, without changing it: `d365_run` (`Get-D365Environment`, `Get-D365Model`),
  `d365_results` on an existing log, every acting tool with
  `dry_run`.
- On the box, changing it (the operator's go each first time): one build of a
  small model, one module sync, one restart.

## Compiled references and dependencies

Compiled references register in this server as one read-only `xref` tool.
[Compiled-reference lookup](XREF_SERVICE.md) explains its four queries and the
difference from source-text search. All nine tools
use the same result/log conventions; xref retains its separate query module,
parameterized fixed SELECTs, binary ordering and connection cleanup.

| Capability | Dependency | Failure scope |
| --- | --- | --- |
| PowerShell operations | Windows PowerShell 5.1 + d365fo.tools | xref and SQL remain available |
| AxDB SQL | pyodbc / Windows SQL authentication | PowerShell and xref remain independent |
| Compiled references | pyodbc / DYNAMICSXREFDB | D365 operations remain available |
| Discovery | none of these external dependencies | always lists all nine tools |

The PowerShell session starts in the background and serializes its own calls.
The pinned MCP 2.2 SDK dispatches synchronous tools on worker threads; xref
queries do not wait for the PowerShell session lock. SQL access is lazy.

## Startup and SQL configuration

For manual startup from the release folder, pass an existing service configuration
(`d365.json` below is a placeholder for its path). Setup normally starts the
service through its scheduled task.

    .\boring-pondnt-d365.cmd serve --config d365.json
    .\boring-pondnt-d365.cmd serve --config d365.json --sql-server localhost --xref-server sql2 --xref-database DYNAMICSXREFDB --xref-driver "ODBC Driver 17 for SQL Server" --xref-stall-seconds 120

`--sql-server` defaults to localhost and supplies xref's default instance.
`--xref-server` overrides only xref. `--xref-database` defaults to DYNAMICSXREFDB;
`--xref-driver` selects a driver (otherwise newest installed SQL Server ODBC,
then Windows SQL Server). `--xref-stall-seconds` defaults to 120; 0 means never.
AxDB remains `d365_sql`'s default database, independently selected per call.

`serve --config config.json` is the only way to start it: authenticated HTTP (boring-service-kit's ServiceConfig): a Crocodile's
introspection and registration with its `access` list as the allowed Ponds
(what tools/devbox-start.ps1 writes), the server's own issuer, or access keys.
setup.ps1 writes the loopback configuration (access keys, 127.0.0.1:18514) and
runs it at logon as the person, elevated. All nine tools in every mode. These Windows-only exceptions do
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

Containment covers the PowerShell process and its descendants. Stopping that
tree does not roll back SQL, a posted transaction or an environment-service
change already requested by the command. Check the actual operation before
retrying it.

### Read the result before retrying

| Observation | What it establishes | Next step |
|---|---|---|
| Warm-session call refused `busy`, command not sent | This call did not reach PowerShell | Observe the current owner and reassess |
| Foreground `timeout` after dispatch | The command was sent; its effects are unknown | Inspect diagnostics and application state before any resubmission |
| Job `running`/`stopping`, or `wait_expired=true` | The same job is still executing or settling | Observe that job with `d365_job` |
| `cleanup_confirmed=false` or `cleanup_unconfirmed` | Process cleanup could not be established | Keep the owning service/job evidence and arrange operator recovery |
| Job `exited` | The process ended and cleanup settled | Check its exit code and the intended build/sync/BP results |

A normal foreground timeout restarts the warm session only after confirming
cleanup of the old tree; it never replays the command. If cleanup fails, the
session retains ownership and blocks restart. A closed session cannot restart.

Each long job has one watcher. A sync remains exclusive while running,
stopping or failed with unconfirmed cleanup. A successor cannot erase its
logs or start over that unresolved sync. Status reads observe the owner;
they do not perform cleanup themselves.

### What owns shutdown

The shared [service-kit process module](https://github.com/the-boring-crocodile/boring-pond-common/blob/main/packages/boring-service-kit/README.md#optional-process-containment)
owns suspended-start Windows Job Objects and the wait/stop/cleanup algorithm.
The process joins its Job Object before resuming. pondNT's Windows bundle
includes the optional process dependency. The portable POSIX test adapter does
not establish Windows-native cleanup.

D365's session owns startup, readiness publication and output-reader/pipe
cleanup. Its job watcher owns each long operation's settlement. Shutdown closes
admission first, requests termination and waits for these owners; it reports
unconfirmed cleanup as failure. Natural parent-process completion also closes
descendants.

The HTTP server lifespan owns normal service shutdown, once per server manager
rather than once per client connection. The CLI's finalizer also closes
resources when transport setup fails before that lifespan starts. Server and
tool construction completes before the warm session starts. Implementation:
[session and jobs](../src/boring_pondnt/d365.py),
[server lifecycle](../src/boring_pondnt/d365_server.py).
