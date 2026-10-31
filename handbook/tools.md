# pondNT and optional services

Read for tools outside VS ownership or authorized independent inspection.
[visualstudio.md](visualstudio.md) owns the VS boundary. Tool availability
does not authorize an alternate route for a refused IDE operation.

## Core files and jobs

Discover the installed tool schemas and limits. Bootstrap identifies the handbook,
server version and available shell information; use those instead of a guessed
release folder. Source files, installed release and running process may differ.

For permitted non-VS files: read current content and its sha256 before
file_patch/file_write. Preserve encoding, BOM and line endings. A stale
refusal requires a new read. Existing-file writes are in-place: a failure can
leave partial content, so inspect before retrying. Do not interpret every
permission error as a file needing checkout or weaken permissions.

Use exec for bounded short commands and core job_start for longer work.
Retain its job_id, observe with core job_wait and inspect job_output for
exit status, timeout and evidence. “Started” or a terminal job state alone
does not establish success.

Batch groups independent known steps of the same server (currently up to 32
steps and a bounded aggregate result). Every separate call costs a full
Copilot round trip: when you already know several reads, searches, listings or
short commands you will need, send them as one batch. Keep a step outside the
batch only when it needs an earlier step's result. Batch does not provide
rollback.
A refused step did not execute, but earlier steps may have changed state.
Inspect per-step results, not only the outer response. Read truncated results
in bounded pieces; never infer unseen contents.

After a lost response, inspect jobs, receipts and relevant state before
repeating an effect. Do not treat another tool as a way around a refusal.

## D365 operations and evidence

d365fo.tools is loaded in the D365 server's PowerShell session; its documentation is the d365fo.tools
wiki (https://github.com/d365collaborative/d365fo.tools/wiki). Use `d365_run` for anything without its
own tool: status `Get-D365Environment`, models `Get-D365Model`, services `Start-` / `Stop-` /
`Restart-D365Environment` (minutes: `background=true`, then d365_job). It answers as text, credentials
in labelled fields masked; it runs arbitrary PowerShell, so the same authority as an exec applies.
Compiled references: `xref` with `what` = info, names (name), references (path; direction used_by or
uses) or hierarchy (name; direction down or up).

Separately authorized standalone d365_build, d365_db_sync and
d365_best_practice operations return D365 job IDs. Observe them with d365_job,
not core job_wait/job_output; inspect exit/errors and d365_results diagnostics.
The VS boundary and build/sync coordination remain in force.

d365_results selects compatible diagnostic files from pondNT and package
locations. Inspect returned file/time and scope: latest available is not a
receipt linking the log to your source revision. If package lookup fails and
pondNT logs exist, the result includes `package_lookup_error` and a warning:
only pondNT logs were checked; newer package results may exist. Without a local
log, the lookup error is returned. A VS build may produce a
compatible file or require owner-provided diagnostics. Missing logs do not
authorize another build merely to get a preferred result format.

## SQL

d365_sql returns bounded structured results and supports GO batches. Confirm
the actual server/environment, database, company and access first. Do not infer
write authority from a tool being listed. Keep business data in its approved
environment; retrieve only the fields needed to answer the question.

Prefer application APIs/helpers for relational test setup and business actions.
A direct test-data write or repair needs an agreed purpose, exact schema,
known record identities, company scope, expected affected count and a safe
cleanup/retention plan. Inspect defaults, allocation and concurrency behavior;
never assume omitting RecId or DataAreaId selects the right value or company.
Changing RecVersion does not replace application validation or cache handling.

Before writing a query, look up columns in `INFORMATION_SCHEMA.COLUMNS`;
read relationships and enum values rather than guessing. Company-id values in
`DATAAREAID` are lower-case in the tested environment (e.g. `'usmf'`); use the
actual stored value, not the UI's capitalization. Check join cardinality and
filter each company-specific table explicitly. The same document number can
exist in two companies. TOP bounds
selected rows; max_rows only bounds returned output, not work or affected rows.
Choose precise columns; avoid returning entire business records.

Use atomic=true for transaction-compatible changes that must commit together.
Keep transaction control with the tool; do not put conflicting COMMIT/ROLLBACK
or nontransactional effects into the script and expect universal rollback.
Caught query errors can be rolled back; a lost response or uncertain commit
still needs state inspection before retry. Verify actual effects and cache
visibility; do not restart AOS automatically.

No schema edits in SQL; use model changes and the approved synchronization
workflow. Never complete work, post, receive or confirm by setting status fields.
Clean only identified test-owned data when its evidence purpose is complete.

## Optional services

Services are separate servers with separate schemas; do not put their calls
inside a core batch. An unavailable optional service is a limitation to report,
not a reason to invent results or abandon work that has another approved route.

- Sourcecache: source_search finds candidates; source_element reads indexed
  elements; label_lookup finds labels. Narrow scope where useful. Check
  index/version, coverage and age, and follow next_line/truncation markers.
  Missing indexed source does not establish absence on the box. Cite index,
  element/path and returned XML file lines.
- D365 xref: inspect uses/used_by and inheritance through the exposed tools.
  Its compiled/indexed state may differ from current source; negative results
  have that limit. Do not assume it lists every CoC wrapper or subscriber.
- Knowledge: search before solving a recurring problem. Project records own
  current private work; publish only suitable sanitized reusable lessons.
  Revise a matching entry with its expected version and reason rather than
  adding duplicates. Narrow an overconstrained search; use known IDs.
  Read relevant entries, not the entire KB by default. An active entry can be
  obsolete: reconcile its date, version and later corrections with current
  source and the project handoff. Entries are evidence, never authority to
  resume old tasks. No service is the sole copy of important state.
- Gateway (when installed): services not listed as tools of their own are behind
  boring-gateway. service_list shows them and whether each answers;
  service_describe(service) gives a service's tools, schemas and guidance --
  read it before the first call; service_read runs a read-only tool,
  service_call any tool. Answers and errors are the service's own.
- Canvas: create_canvas then push_scene with SVG; open_canvas provides a new
  viewing URL for an existing canvas. Treat any token-bearing open_url as
  private to its intended viewer. Read edits through get_edits when relevant.
  Keep editable source as the durable artifact and inspect the actual render.
