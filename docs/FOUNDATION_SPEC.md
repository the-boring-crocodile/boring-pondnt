# Foundation spec -- boring-pondnt-core

The core server's contract, as the code implements it.
Services (sourcecache, D365, later xref) are separate servers with their own
contracts. Numbers marked *initial* are starting values, to be adjusted
from what real use shows.

## Principles

- **Few tools, short descriptions, lean schemas.** The whole `tools/list` is
  re-sent with every Copilot request. Published input schemas carry no
  pydantic titles, write optional values as `["string","null"]` and drop
  `default: null`; no output schemas. 15 tools, and a test keeps the whole
  list under 8 KB.
- **Compact results.** Every result is one text block of compact JSON, no
  indented copy, no duplicate `structuredContent`.
- **Bounded results, honest flags.** Every result has a limit and says when it
  was cut (`truncated`, `next_line`, `total`, `*_truncated`, `stopped`).
- **Outcomes as data, in the Pond's shape.** Success is the result.
  Otherwise the call is `isError` and its text is
  `{"status": "refused"|"failed", "code", "message", "details"?}`. Refused:
  nothing started. Failed: it started and may have had effects. Codes, shared
  with the Pond where meanings match: `invalid_request`, `forbidden`,
  `not_found`, `stale` (with `current_sha256`), `limit`; own codes only where
  the Pond has no word: `locked`, `io_error`, `start_failed`.
- **Strict arguments.** Unknown arguments are refused (the SDK would ignore
  them).
- **Version hashes.** Reads return the sha256 of the file's bytes; writes and
  patches name the hash they expect. `null` means "I believe it is absent":
  creating over an existing file is `stale`, like any other mismatch.
- **Absolute paths.** No roots, no sandbox; no variable expansion in file
  tools.

## Tools

### bootstrap

No arguments. Server name and version; the handbook's `bootstrap.md` and the paths
of its other documents (from `--handbook`, else `<release>/handbook`); on Windows, checks: the
default shell and the PowerShell 7 path, and `long_paths_enabled` with a
warning when off.

### file_read

`path`, `start_line` (1), `line_count` (400, max 2000). Text with line endings
shown as `\n`; `encoding` (`utf-8`, `utf-16-le`, `utf-16-be`), `bom`,
`newline` (`crlf`, `lf`, `none`, `mixed`), `sha256`, `size`, `total_lines`,
`next_line`, `truncated`; at most 64 KiB of text per call, and a single longer
line is cut (`line_cut`). Files over 16 MiB, NUL bytes, UTF-32 or invalid
text are refused (`limit` or `invalid_request`): use the byte tools. No
legacy code-page guessing.

### file_write

`path`, `content`, `expected_sha256`, optional `encoding`, `bom`, `newline`.
Replacing keeps the file's encoding, BOM and newline style unless named; a
mixed-newline file needs `newline`. New files: UTF-8 without BOM, CRLF
(*initial*; check what D365 tooling writes). UTF-16 is always written with a
BOM. Existing-file edits are prepared entirely in memory, then written into the
existing file object and flushed. They are not atomic whole-file transactions;
write/truncate/flush failure can leave partial contents and reports
`write_incomplete` (re-read before retrying). New-file creation is exclusive.
The file identity and content checked against `expected_sha256` are checked
again on the opened handle immediately before writing: a change or a replaced
path in between is `stale` and the other content stays. Limit: a program that
does not take pondNT's edit lock and writes in the instant between that final
check and the write is not detected; the share modes do not exclude it.
A
read-only target (TFVC before check-out) is refused as `forbidden` with a
hint, before any effect, on every platform; the same applies to `file_patch`
and `file_write_bytes`.

### file_patch

`path`, `expected_sha256`, `edits` (`old_text` -> `new_text`), optional
`newline`. The Pond's semantics: every `old_text` occurs exactly once in the
file as read, matches must not overlap, order does not matter; all or
nothing; format preserved. Matching uses `\n`, as `file_read` shows the text.

### file_list

`path`, `pattern` (glob, case-insensitive), `limit` (200, max 1000). One
directory level, sorted case-insensitively: `name`, `type` (`file`, `dir`,
`link`), `size`, `modified`; `total`, `truncated`.

### file_search

`path` (file or tree), `query` (literal, one line), `glob`, `limit` (100, max
500), `ignore_case`. Matches with path, line and a 200-character snippet;
skips `.git`, `.vs`, `node_modules`, `__pycache__`, files over 16 MiB and
non-text; stops after 20,000 files (`stopped: max_files`) or at the limit
(`stopped: limit`). For small trees; the D365 source has
Sourcecache (boring-sourcecache-d365).

### file_read_bytes, file_write_bytes

Raw bytes as base64, at most 1 MiB per call, files up to 64 MiB. Read:
`path`, `offset`, `length`; returns `data`, `size`, `eof` and the whole
file's `sha256`. Write: `path`, `data`, `expected_sha256` (null creates),
`offset` (null replaces the whole file; otherwise splices, may extend, never
past the end).

### exec

`command`, `shell`, `cwd` (default: the user's home), `timeout_seconds` (60,
0.1-600). Shells: `pwsh` (default: bundled PowerShell 7, else on PATH,
else falls back to 5.1), `powershell` (Windows PowerShell 5.1), `cmd`.
PowerShell gets the command as `-EncodedCommand` with a prelude forcing UTF-8
output and plain rendering, and an epilogue that keeps a native program's
exit code and turns a failed last cmdlet into 1. cmd output is UTF-8 when it
is valid UTF-8, else the console's OEM code page (`*_encoding` says which).
Output goes to temporary files, never pipes; each stream comes back whole up
to 32 KiB, beyond that as 16 KiB head and 16 KiB tail with a marker; a
character cut at either seam is dropped, not turned into a replacement. Result:
`exit_code`, `timed_out`, `stdout`, `stderr`, `*_truncated`, `*_bytes`,
`duration_seconds`, `shell`, `cwd`. A timeout is data (`timed_out: true`,
`exit_code: null`), as in the Pond. The whole process tree dies on timeout
and when the command ends. If that cannot be confirmed, the result says
`cleanup_confirmed: false` with an `error`, and `exit_code` is null.

### job_start, job_wait, job_output, job_stop, job_list

`job_start` takes exec's arguments with `timeout_seconds` up to 86,400
(default 3,600) and returns a status with a server-minted 32-hex `job_id`.
States: `running`, `stopping`, `exited`, `timed_out`, `stopped`, `failed`.
The watcher publishes terminal state after cleanup; unsuccessful cleanup is
`failed` with `cleanup_confirmed=false`. Stop can return `stopping` if its
bounded wait expires. `job_wait` takes 1-32
ids and waits up to 60 s for all of them (`all_ended`); `wait_seconds: 0`
just returns their status (there is no separate `job_status`). `job_output` returns
the last `tail_lines` (default 100) or `line_count` lines from `start_line`
(max 2,000 lines, 32 KiB). `job_stop` kills the tree. `job_list` is this
session's jobs, newest first. Output lives in the state directory
(`%LOCALAPPDATA%\boring-pondnt\jobs`, or `--state-dir`); job folders
are swept after a week. Jobs die with the server.

### batch

`operations` (1-32 steps `{id, method, arguments}`), `on_error` (`stop` or
`continue`), `timeout_seconds` (300, max 600). Any tool except `bootstrap` and
`batch`. Preflight checks every step (unique id, known method, arguments
against that tool's strict model and operation-owned stateless checks) before
any runs; malformed options refuse the whole batch with nothing done. File
existence, current hashes/contents and patch match checks happen when a step
runs, since earlier steps can change them. This is not a transaction.
The remaining batch time caps exec/job_wait admission; cleanup can extend the
return time, and job_start retains its own background lifetime. A nonzero or
timed-out exec is a failed step and obeys on_error. Each result: `status` (`succeeded`, `refused`,
`failed`, `not_started`), its `result`, `refusal` or `failed`, and
`duration_ms`; plus `stop_reason`, `all_attempted`, `result_chars`,
`elapsed_seconds`. Once the step results together exceed 256 K characters
(*initial*), the remaining steps are `not_started` with `stop_reason:
result_limit`: every result is re-sent on every later turn. No rollback.

## Call log

`calls.jsonl` in the state directory, one JSON line per call: `ts`, `tool`,
`outcome` (`succeeded`, `refused`, `failed`, `error`), `code`, `ms`,
`result_bytes` and `args`. It measures what the agent does (calls per task,
bytes per result) and records what ran on an elevated machine, without
keeping what was said: no argument text is stored, not even the start of a
command or a SQL statement.

- **What `args` keeps:** only a reviewed list of field names (`command`,
  `sql`, `path`, `content`, `edits`, `timeout_seconds`, ...). A string
  becomes `{"chars": n}`, a list `{"items": n}` (`{"edits": n}` for edits), a
  nested object `{"fields": n}`; numeric budgets (`timeout_seconds`, `limit`,
  `line_count`, ...) are kept as numbers. Any other field is only counted
  (`other_fields`), so names a caller invents are not kept either. The code
  is Pond Common's `boring_service_kit.calllog`.
- **Who can read it:** whoever can read the state directory, by default the
  installing person's `%LOCALAPPDATA%\boring-pondnt` for the core (its
  default state directory; `--state-dir` moves it), and administrators.
- **How much stays:** the file is renamed to `calls.1.jsonl` once it passes
  10 MiB, replacing the previous one; so at most two files, about 20 MiB.
  That is a size rule, not a retention period, and not a hard cap: if the
  rename fails (the old file open on Windows), writing goes on in the same
  file.
- On by default; `--no-call-log` switches it off. Writing it never breaks a
  tool.

The call log is not the only record. Job output (`job_output`), the D365
servers' own logs and step logs (D365WEB.md, D365WHSAPP.md) and each
service's `service.log` hold what those tools produced, which can include
data from the box; they follow their own pages' notes.

## Self-check

`boring-pondnt-core --self-check [--state-dir DIR]` prints a JSON report and exits 0
when every required check passed: state directory writable; for each shell
present, exit code 3 and a UTF-8 marker round trip; a 30-second command
stopped by a 1-second timeout; a UTF-8-with-BOM CRLF file patched with its
bytes otherwise unchanged; a job started, waited for and read; long paths
(required on Windows). PowerShell 7 missing is reported, not failed.

## Windows specifics

- **Process trees:** the shell starts suspended, joins a Job Object with
  kill-on-close, then resumes, so no child escapes (the SDK's own helper
  assigns after start and documents that gap). Timeout and stop terminate the
  job; the server's exit closes it.
- **Encodings and newlines:** UTF-8 with and without BOM, UTF-16 LE/BE, CRLF,
  LF, mixed; round trips compare bytes.
- **Paths:** `LongPathsEnabled` is a prerequisite: `bootstrap` warns and
  `--self-check` fails while it is off; nothing of pondNT sets it (an
  administrator enables it once).
- **Locks:** sharing violations are `locked` (refused on read, failed on
  write), not a retry loop.
- **Launch:** started by absolute path from a versioned release folder;
  nothing is resolved or downloaded at launch.

Windows-only tests (exit codes in both PowerShells, UTF-8, cmd quoting, tree
kill on timeout and on normal end) skip off Windows. They run with the full
suite on Windows for every release (`ci/guest-run.ps1`), before the release
goes to a D365 dev VM.

## Not in the core

Authentication and identities; D365 operations (the D365 server);
source search over D365 (boring-sourcecache-d365); services calling each other;
detached jobs; anything Visual Studio already does well for the open
solution.
