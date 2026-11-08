"""D365 tool registration and diagnostic presentation; lifecycle belongs to the server."""
from __future__ import annotations
from typing import Callable
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Annotated, Any
from . import __version__
from . import d365_logs as log_sources
from . import sqlserver
from pydantic import Field, StrictInt
from .d365 import NAME, TAIL_LINES_DEFAULT, TAIL_LINES_MAX, WAIT_SECONDS_MAX, Jobs, Session, check_tail_lines, checked_name, ps_quote
from boring_service_kit.tools import READ_ONLY, RUNS, WRITES
from boring_service_kit.outcomes import Failed, Outcome, Refused

INSTRUCTIONS = (
    "Build, sync and best practice for this D365 dev box, with d365fo.tools loaded in one PowerShell session: "
    "d365_run runs any d365fo.tools command there (status: Get-D365Environment; models: Get-D365Model). "
    "Source text and labels: boring-sourcecache-d365. Compiled references: xref. Long operations run as jobs (d365_job)."
)
COMPILER_ROWS_DEFAULT, COMPILER_ROWS_MAX = 50, 500
DRY_RUN_CHARS = 4000
MESSAGE_CHARS = 500
RESULT_FILES = {"compile": "Dynamics.AX.*.xppc.xml", "best_practice": "Dynamics.AX.*.xppbp.xml"}
KINDS = (*RESULT_FILES, "sync")
SYNC_STEPS_MAX = 60
SYNC_LOGS = ("SyncEngine.exe_ErrOutput.log", "SyncEngine.exe_StdOutput.log")
RUN_COMMAND_CHARS = 20_000
RUN_OUTPUT_DEFAULT, RUN_OUTPUT_MAX = 20_000, 200_000
RUN_TIMEOUT_DEFAULT, RUN_TIMEOUT_MAX = 60, 600

_SECRET_NAME = r"[a-z0-9_]*(?:password|passwort|pwd|secret|accountkey|sharedaccesskey|sig|token|apikey|api_key|credential)"
_SECRET_KEY = re.compile(r"(?i)^" + _SECRET_NAME + r"$")
_SECRET = re.compile(
    # OData's $skiptoken is a continuation cursor; masking it breaks paging.
    r"(?i)\b((?!(?<=\$)skiptoken(?=[\"']?\s*[=:]))" + _SECRET_NAME + r")([\"']?\s*[=:]\s*|\s+)"
    r"('(?:[^']|'')*'|\"[^\"]*\"|'[^']*$|\"[^\"]*$|[^\s\"'&]+)")

# Credentials recognised by their shape, wherever they stand: a JWT (Entra access tokens), a bearer
# header, a pondNT/Pond access key.
_SECRET_SHAPES = (
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), "***"),
    # a bearer value looks like a token: long, or with digits or token punctuation ("Bearer authentication" is prose)
    (re.compile(r"(?i)\b(bearer)\s+(?=[A-Za-z0-9._~+/=-]{20,}|[A-Za-z]*[0-9._~+/=-])[A-Za-z0-9._~+/=-]{8,}"), r"\1 ***"),
    (re.compile(r"\bbsk_[A-Za-z0-9_-]{8,}"), "bsk_***"),
)


def _mask(match: re.Match[str], text: str) -> str:
    """name=value and name: value always; name value (a space only) for a switch (-Password x) or a
    value that looks secret (a digit, 16+ characters): "the token expired at noon" stays prose."""
    name, separator, secret = match.group(1), match.group(2), match.group(3)
    if not separator.strip():
        switch = match.start(1) > 0 and text[match.start(1) - 1] == "-"
        if not (switch or any(c.isdigit() for c in secret) or len(secret) >= 16 or secret[:1] in "'\""):
            return match.group(0)
    return f"{name}{separator}***"


def redact(value: Any) -> Any:
    """Mask recognized credential fields; arbitrary unlabeled prose is not detectable."""
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                structured = json.loads(value)
            except (ValueError, RecursionError):
                pass
            else:
                return json.dumps(redact(structured), ensure_ascii=False)
        # Unquoted semicolons are ambiguous: mask the whole contiguous value
        # rather than leaking a password suffix as if it were another field.
        value = _SECRET.sub(lambda m: _mask(m, value), value)
        for shape, mask in _SECRET_SHAPES:
            value = shape.sub(mask, value)
        return value
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: "***" if isinstance(key, str) and _SECRET_KEY.fullmatch(key)
                else redact(item) for key, item in value.items()}
    return value


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def diagnostics(path: Path, module: str, kind: str, warnings: bool, limit: int) -> dict[str, Any]:
    """Rows from a diagnostics file -- xppc's and xppbp's share the format (what Visual Studio reads):
    totals by severity, then errors (and warnings)."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as error:
        raise Failed("unreadable", f"cannot read the {kind} results: {error}", path=str(path)) from None
    totals: dict[str, int] = {}
    rows = []
    wanted = {"error", "warning"} if warnings else {"error"}
    for item in root.iter("Diagnostic"):
        field = {child.tag: (child.text or "") for child in item}
        severity = field.get("Severity", "")
        totals[severity] = totals.get(severity, 0) + 1
        if severity.lower() in wanted:
            rows.append({"severity": severity, "type": field.get("DiagnosticType"),
                         "element_type": field.get("ElementType"), "path": field.get("Path"),
                         "line": int(field["Line"]) if field.get("Line", "").isdigit() else None,
                         "column": int(field["Column"]) if field.get("Column", "").isdigit() else None,
                         "message": field.get("Message", "")[:MESSAGE_CHARS]})
            if field.get("ItemSpecific"):
                rows[-1]["item"] = field["ItemSpecific"][:MESSAGE_CHARS]
    rows.sort(key=lambda r: (r["severity"].lower() != "error", r["path"] or "", r["line"] or 0))
    written = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(path.stat().st_mtime))
    return {"module": module, "kind": kind, "file": str(path), "written": written, "totals": totals,
            "rows": rows[:limit],
            "shown": min(len(rows), limit), "truncated": len(rows) > limit}


# SyncEngine's logs as d365fo.tools leaves them (-LogPath): an entry starts with "Log level - <Level> |",
# may run on over a .NET stack trace and ends with "' on category '<Category>'.".
_SYNC_ENTRY = re.compile(r"^\s*Log level - (\w+) \|\s*(?:Infolog diagnostic message:\s*)?'?(.*)$")
_SYNC_CATEGORY = re.compile(r"' on category '(\w+)'\.?\s*$")
_SYNC_STEP = re.compile(r"^\S+ (.+?) finished\. Time elapsed: ([\d:.]*\d)")
_SYNC_TOTAL = re.compile(r"^Sync finished and took (\d+) milliseconds")
_SYNC_MODE = re.compile(r"^SyncMode: (\S+)")
# Lines that are never part of an entry's message: an entry without its category line ends there.
_SYNC_BOUNDARY = re.compile(r"^\s*(\d{4}-\d\d-\d\dT|\[DbSync:|Sync finished|SyncMode:|=== )")
_SYNC_STACK = re.compile(r"^\s*at [\w.`<>]+[(.]")


def _sync_message(lines: list[str]) -> str:
    """The message without the .NET stack trace: stack lines start with `at Type.Method(`; the first
    stack frame may follow `Message -` on the entry's own line."""
    kept = [lines[0]] + [line for line in lines[1:] if not _SYNC_STACK.match(line)]
    kept[0] = re.split(r"\s*Message -\s+at [\w.`<>]+[(.]", kept[0], maxsplit=1)[0]
    text = _SYNC_CATEGORY.sub("", " ".join(line.strip() for line in kept))
    return re.sub(r"\s*Message -\s*$", "", text).strip()[:MESSAGE_CHARS]


def _sync_entries(lines: list[str]) -> list[tuple[str, str, str | None]]:
    """(level, message, category) per logged entry; an entry's text runs until its category line."""
    entries: list[tuple[str, str, str | None]] = []
    level: str | None = None
    text: list[str] = []

    def close(category: str | None) -> None:
        if level is not None:
            entries.append((level, _sync_message(text), category))

    for line in lines:
        entry = _SYNC_ENTRY.match(line)
        if entry:
            close(None)  # the previous entry had no category line
            level, text = entry.group(1), [entry.group(2)]
        elif level is not None and _SYNC_BOUNDARY.match(line):
            close(None)
            level, text = None, []
            continue
        elif level is not None:
            text.append(line)
        if level is not None:
            category = _SYNC_CATEGORY.search(line)
            if category:
                close(category.group(1))
                level, text = None, []
            elif len(text) > 200:  # never swallow the rest of the log
                close(None)
                level, text = None, []
    close(None)
    return entries


def sync_results(folder: Path, warnings: bool, limit: int) -> dict[str, Any]:
    """The last sync from SyncEngine's own logs: mode, steps with their time, total, then the logged
    errors (and warnings) as rows -- SyncEngine runs with ContinueOnError, so exit 0 can hide errors."""
    paths = [folder / name for name in SYNC_LOGS if (folder / name).is_file()]
    if not paths:
        raise Refused("not_found", "no sync logs here; run d365_db_sync first", folder=str(folder))
    found: dict[tuple[str, str], dict[str, Any]] = {}
    summary: dict[str, Any] = {"mode": None, "steps": [], "milliseconds": None}
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError as error:
            raise Failed("unreadable", f"cannot read {path.name}: {error}", folder=str(folder)) from None
        counts: dict[tuple[str, str], int] = {}
        for level, message, category in _sync_entries(lines):
            key = (level, message)
            counts[key] = counts.get(key, 0) + 1
            row = found.setdefault(key, {"level": level, "category": None, "message": message})
            row["category"] = row["category"] or category
        if path.name.endswith("StdOutput.log"):
            for line in (line.strip() for line in lines):
                for pattern, field in ((_SYNC_MODE, "mode"), (_SYNC_TOTAL, "milliseconds")):
                    hit = pattern.match(line)
                    if hit:
                        summary[field] = int(hit.group(1)) if field == "milliseconds" else hit.group(1)
                step = _SYNC_STEP.match(line)
                if step and len(summary["steps"]) < SYNC_STEPS_MAX:
                    summary["steps"].append({"step": step.group(1), "elapsed": step.group(2)})
        for key, count in counts.items():  # each log repeats the other's errors: count once
            found[key]["count"] = max(found[key].get("count", 0), count)
    totals: dict[str, int] = {}
    for row in found.values():
        totals[row["level"]] = totals.get(row["level"], 0) + row["count"]
    wanted = {"error", "warning"} if warnings else {"error"}
    rows = sorted((r for r in found.values() if r["level"].lower() in wanted),
                  key=lambda r: (r["level"].lower() != "error", r["message"]))
    newest = max(p.stat().st_mtime for p in paths)
    return {"kind": "sync", "folder": str(folder),
            "written": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(newest)),
            **summary, "totals": totals, "rows": rows[:limit], "shown": min(len(rows), limit),
            "truncated": len(rows) > limit}


def register_tools(tool: Callable[..., Any], session: Session, jobs: Jobs,
                   state: Path, sql_connect: Callable[..., Any], call_budget: float | None = None,
                   guard: Callable[[Callable[..., Any], str], Callable[..., Any]] = lambda fn, role: fn) -> None:
    """`call_budget` (behind a Pond, ServiceConfig.call_limit): no call may outlast it.
    `guard(fn, role)` authorizes one action inside a tool that needs a higher role than the tool."""
    def bounded(seconds: float) -> float:
        return min(seconds, call_budget) if call_budget else seconds

    wait_max = bounded(WAIT_SECONDS_MAX)
    run_max = int(bounded(RUN_TIMEOUT_MAX))
    sql_max = int(bounded(sqlserver.TIMEOUT_MAX))

    def dry(script: str) -> dict[str, Any]:
        """d365fo.tools' -OutputCommandOnly writes the command straight to the host (no stream): take
        those lines from the session's stdout. They carry SQL passwords -- `tool` masks the result."""
        value, host = session.call(f"{script} -OutputCommandOnly", with_host=True)
        lines = [str(line) for line in host + _as_list(value) if str(line).strip()]
        command = redact("\n".join(lines).strip())  # before cutting: a cut must never split a password
        result: dict[str, Any] = {"dry_run": True, "command": command[:DRY_RUN_CHARS], "chars": len(command)}
        if len(command) > DRY_RUN_CHARS:
            result["truncated"] = True  # a module sync lists every entity: ~0.5 MB on the dev box
        return result

    def act(kind: str, script: str, dry_run: bool) -> dict[str, Any]:
        if dry_run:
            return dry(script)
        session.ensure_ready()
        return jobs.start(kind, script)

    @tool(RUNS, "writer")
    def d365_build(
        module: Annotated[str, Field(
            description="D365 package name.",
            json_schema_extra={"pattern": f"^{NAME.pattern}$"},
        )],
        include_reports: Annotated[bool, Field(description="Also build reports.")] = False,
        dry_run: Annotated[bool, Field(description="Show the command without starting the job.")] = False,
    ) -> dict[str, Any]:
        """Start a package build job. Poll d365_job, then read diagnostics with d365_results(kind=compile)."""
        name = checked_name("module", module)
        logs = state / "d365-logs" / name
        if not dry_run:
            logs.mkdir(parents=True, exist_ok=True)
        script = f"Invoke-D365ModuleBuild -Module {ps_quote(name)} -LogPath {ps_quote(str(logs))}"
        if include_reports:
            script += " -IncludeReports"
        return act("build", script, dry_run)

    @tool(READ_ONLY, "reader")
    def d365_results(
        module: Annotated[str | None, Field(
            description="Package name. Required for compile and best_practice. Ignored for sync.",
        )] = None,
        kind: Annotated[str, Field(json_schema_extra={"enum": KINDS})] = "compile",
        warnings: Annotated[bool | None, Field(
            description="Include warnings. Defaults to true for best_practice, false otherwise.",
        )] = None,
        limit: Annotated[int, Field(
            description="Maximum diagnostic rows.",
            json_schema_extra={"minimum": 1, "maximum": COMPILER_ROWS_MAX},
        )] = COMPILER_ROWS_DEFAULT,
    ) -> dict[str, Any]:
        """Read the latest compile, best-practice or sync diagnostics. Match the result's file and time to your run."""
        if not 1 <= limit <= COMPILER_ROWS_MAX:
            raise Refused("invalid_request", f"limit must be 1-{COMPILER_ROWS_MAX}")
        if kind not in KINDS:
            raise Refused("invalid_request", "kind is compile, best_practice or sync", kind=kind)
        if kind == "sync":  # one sync engine per box: the last run, whatever it synced
            result = sync_results(state / "d365-logs" / "sync", bool(warnings), limit)
            pending = jobs.pending("sync")
            if pending:
                result["note"] = f"sync job {pending[0]} has not settled: these results are partial"
            return result
        if not module:
            raise Refused("invalid_request", f"{kind} results need the module")
        name = checked_name("module", module)
        pattern = RESULT_FILES[kind]
        found = list((state / "d365-logs" / name).glob(f"**/{pattern}"))
        # A newer IDE build can coexist with older pondNT logs. Search both owners;
        # a log's freshness still does not associate it with a particular source revision.
        package_path = None
        lookup_error = None
        try:
            package_path = session.call(
                f"$p = (Get-D365InstalledPackage -Name {ps_quote(name)}).PackageDirectory; "
                f"if ($p) {{ Get-ChildItem $p -Filter '{pattern}' -Recurse -Depth 2 -ErrorAction "
                "Stop | Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName }")
        except (Refused, Failed) as error:
            if not found:
                raise
            # Reading retained logs does not require a working PowerShell session.
            # Do not expose raw command/session output or claim both locations were checked.
            lookup_error = error.code
        if package_path:
            found.append(Path(package_path))
        if not found:
            raise Refused("not_found", f"no compatible {kind} results found; inspect the actual build/check "
                          "diagnostics in Visual Studio before deciding whether another run is needed",
                          module=module)
        try:
            path = max(found, key=lambda p: p.stat().st_mtime_ns)
        except OSError as error:
            raise Failed("unreadable", f"cannot select the latest {kind} results: {error}") from None
        # best practice reports only warnings: show them unless asked not to
        show_warnings = (kind == "best_practice") if warnings is None else warnings
        result = diagnostics(path, module, kind, show_warnings, limit)
        result["log_source"] = "package" if package_path and path == Path(package_path) else "pondnt"
        result["note"] = ("Latest available diagnostic file, not proof of a completed current build/check. "
                          "Match its path, time and scope to the intended run and source state.")
        if lookup_error:
            result["package_lookup_error"] = lookup_error
            result["note"] += (f" Package lookup failed ({lookup_error}); only pondNT logs were checked. "
                               "A newer package diagnostic may exist.")
        return result

    @tool(RUNS, "writer")
    def d365_db_sync(
        modules: Annotated[list[str] | None, Field(
            description="Package names. Omit or pass an empty list for a full sync.",
            json_schema_extra={"items": {"type": "string", "pattern": f"^{NAME.pattern}$"}},
        )] = None,
        dry_run: Annotated[bool, Field(description="Show the command without starting the job.")] = False,
    ) -> dict[str, Any]:
        """Start a database sync job. Poll d365_job, then read the sync log with d365_results(kind=sync)."""
        logs = state / "d365-logs" / "sync"
        if modules:
            names = ",".join(ps_quote(checked_name("module", m)) for m in modules)
            script = f"Invoke-D365DbSyncModule -Module {names}"
        else:
            script = "Invoke-D365DbSync"
        def prepare() -> None:
            logs.mkdir(parents=True, exist_ok=True)
            try:
                for name in SYNC_LOGS:  # a sync that dies early must not show the last run's results
                    (logs / name).unlink(missing_ok=True)
            except OSError as error:  # a sync started elsewhere (Visual Studio) may hold them
                raise Refused("locked", f"the last sync's logs are in use: {error}") from None
        script = f"{script} -LogPath {ps_quote(str(logs))}"
        if dry_run:
            return dry(script)
        session.ensure_ready()
        return jobs.start("sync", script, prepare=prepare)

    @tool(RUNS, "writer")
    def d365_best_practice(
        module: Annotated[str, Field(
            description="D365 package name.",
            json_schema_extra={"pattern": f"^{NAME.pattern}$"},
        )],
        model: Annotated[str, Field(
            description="Model within the package.",
            json_schema_extra={"pattern": f"^{NAME.pattern}$"},
        )],
        dry_run: Annotated[bool, Field(description="Show the command without starting the job.")] = False,
    ) -> dict[str, Any]:
        """Start a model check job. Poll d365_job, then call d365_results(kind=best_practice)."""
        name = checked_name("module", module)
        logs = state / "d365-logs" / name
        if not dry_run:
            logs.mkdir(parents=True, exist_ok=True)
        script = (f"Invoke-D365BestPractice -Module {ps_quote(name)} -Model {ps_quote(checked_name('model', model))} "
                  f"-LogPath {ps_quote(str(logs))}")
        return act("best_practice", script, dry_run)

    @tool(RUNS, "writer")
    def d365_run(
        command: Annotated[str, Field(
            description="d365fo.tools command or short PowerShell script.",
            json_schema_extra={"minLength": 1, "maxLength": RUN_COMMAND_CHARS},
        )],
        timeout: Annotated[int, Field(
            description=(
                "Foreground seconds, 1–600 with a lower service cap where configured. Ignored in "
                "the background."
            ),
        )] = min(RUN_TIMEOUT_DEFAULT, run_max),
        background: Annotated[bool, Field(description="Return a job ID instead of waiting for output.")] = False,
        max_chars: Annotated[int, Field(
            description="Maximum foreground output characters.",
            json_schema_extra={"minimum": 1, "maximum": RUN_OUTPUT_MAX},
        )] = RUN_OUTPUT_DEFAULT,
    ) -> dict[str, Any]:
        """Run PowerShell in the D365 session. For a background run, poll d365_job. Output masks labelled credentials."""
        # d365fo.tools: https://github.com/d365collaborative/d365fo.tools/wiki. Labelled credentials
        # (password=, pwd, secret, sig, ...) are masked before the output is cut; unlabelled text cannot be.
        if not command.strip() or len(command) > RUN_COMMAND_CHARS:
            raise Refused("invalid_request", f"command: d365fo.tools commands or a short script, up to "
                          f"{RUN_COMMAND_CHARS} characters")
        if not background and not 1 <= timeout <= run_max:
            raise Refused("invalid_request", f"timeout must be 1-{run_max} seconds here; longer: background=true")
        if not 1 <= max_chars <= RUN_OUTPUT_MAX:
            raise Refused("invalid_request", f"max_chars must be 1-{RUN_OUTPUT_MAX}")
        if background:
            session.ensure_ready()
            return jobs.start("run", command)
        value, host = session.call(f"& {{\n{command}\n}} | Out-String -Width 220", timeout=timeout,
                                   with_host=True)
        lines = [str(line) for line in host + _as_list(value) if str(line).strip()]
        text = redact("\n".join(lines).strip())  # before cutting: a cut must never split a password
        result: dict[str, Any] = {"output": text[:max_chars], "chars": len(text)}
        if len(text) > max_chars:
            result["truncated"] = True
        return result

    # Reading a job is reader work (THREAT_MODEL: readers read jobs); stopping one changes state.
    stop_job = guard(jobs.stop, "writer")

    @tool(WRITES, "reader")
    def d365_job(
        job_id: Annotated[str | None, Field(
            description="ID returned by a D365 job tool.",
            json_schema_extra={"pattern": "^[0-9a-f]{12}$"},
        )] = None,
        tail_lines: Annotated[int, Field(
            description="Output lines to keep, 1–400 when a job ID is given.",
        )] = TAIL_LINES_DEFAULT,
        stop: Annotated[bool, Field(description="Request termination. Requires job_id and zero wait_seconds.")] = False,
        wait_seconds: Annotated[float, Field(
            description=(
                "Seconds to wait for one job. Zero reads status immediately. A service may set a "
                "lower limit."
            ),
            json_schema_extra={"minimum": 0, "maximum": WAIT_SECONDS_MAX},
        )] = 0,
    ) -> dict[str, Any]:
        """Read jobs or wait for one to finish. Omit job_id to list. stop changes job state and needs a writer."""
        if not 0 <= wait_seconds <= wait_max:
            raise Refused("invalid_request", f"wait_seconds must be 0-{wait_max:g} here")
        if job_id is None:
            if stop or wait_seconds != 0:
                raise Refused("invalid_request", "stop/wait requires job_id")
            return {"jobs": jobs.list()}
        if stop and wait_seconds != 0:
            raise Refused("invalid_request", "choose stop or wait, not both")
        check_tail_lines(tail_lines)  # refuse invalid output options before stop can have effects
        status = stop_job(job_id) if stop else jobs.wait(job_id, wait_seconds)
        return {**status, "tail": jobs.tail(job_id, tail_lines)}

    @tool(READ_ONLY, "reader")
    def d365_logs(
        source: Annotated[log_sources.LogSource, Field(description=(
            "batch reads job/task history and infologs. process reads execution messages. iis reads AOSService W3C "
            "logs. events selects WAS, Application Error, .NET Runtime, IIS-W3SVC-WP, Dynamics*, Management Reporter* "
            "or text naming AOSService/Batch.exe/w3wp in Application/System. Application Error needs a D365 app/module."))],
        since: Annotated[StrictInt | str | None, Field(description=(
            "Start inclusive, 1–43,200 minutes back or an ISO time in the past 30 days. Default 60 minutes. Use ISO since/until for the same window across calls. Minutes move per call."))] = None,
        level: Annotated[log_sources.LogLevel, Field(description=(
            "Minimum severity: warning includes errors, all includes every level. For batch, error matches "
            "status Error or error infolog entries. A Finished task can carry errors. "
            "Use status=error for failed tasks only."))] = "error",
        contains: Annotated[str | None, Field(description=(
            "Case-insensitive substring of text, captions, process names or IIS paths. At most 1,000 characters."))] = None,
        group: Annotated[bool, Field(description=(
            "Group repeats with first, last, distinct variants. count counts records/tasks, not distinct business "
            "failures. Normalize numbers, GUIDs and times."))] = True,
        limit: Annotated[StrictInt, Field(description="Maximum result rows, 1–500.",
                                         json_schema_extra={"minimum": 1, "maximum": log_sources.LIMIT_MAX})] = log_sources.LIMIT_DEFAULT,
        max_chars: Annotated[StrictInt, Field(description=(
            "Maximum JSON row-array characters, 1–262,144."),
                  json_schema_extra={"minimum": 1, "maximum": log_sources.CHARS_MAX})] = log_sources.CHARS_DEFAULT,
        until: Annotated[StrictInt | str | None, Field(description=(
            "End exclusive, 1–43,200 minutes back or an ISO time in the past 30 days. Defaults to now."))] = None,
        job: Annotated[str | None, Field(description=(
            "Batch or process only. Case-insensitive substring of a job/task caption or process name, up to 200 characters."))] = None,
        status: Annotated[str | None, Field(description=(
            "Batch only. Case-insensitive task status (" + ", ".join(log_sources.BATCH_STATUS_CODES) + ")."))] = None,
        until_skip: Annotated[StrictInt | None, Field(description=(
            "Scan-note cursor: includes until, skips this many rows already seen there."),
            json_schema_extra={"minimum": 0, "maximum": log_sources.SKIP_MAX})] = None,
    ) -> dict[str, Any]:
        """Read one D365 log source (UTC). Read-only. A cut scan's note gives until/until_skip."""
        result = log_sources.read_logs(source, since, level, contains, group, limit, max_chars,
                                       sql_connect, bounded(20), redact, session, until=until, job=job, status=status,
                                       until_skip=until_skip)
        return {"server": {"name": "d365", "version": __version__}, **result}

    @tool(WRITES, "writer")
    def d365_sql(
        sql: Annotated[str, Field(
            description="SQL script. Put GO on its own line between batches.",
            json_schema_extra={"minLength": 1, "maxLength": 1_000_000},
        )],
        max_rows: Annotated[int, Field(
            description="Maximum rows per result set.",
            json_schema_extra={"minimum": 1, "maximum": sqlserver.ROWS_MAX},
        )] = sqlserver.ROWS_DEFAULT,
        timeout: Annotated[int, Field(
            description=(
                "Seconds for the entire script. A service may set a lower limit and shorten the "
                "60-second default."
            ),
            json_schema_extra={"minimum": 1, "maximum": sqlserver.TIMEOUT_MAX},
        )] = sqlserver.TIMEOUT_DEFAULT,
        atomic: Annotated[bool, Field(description="Run all batches in one transaction.")] = False,
        database: Annotated[str, Field(
            description="SQL database on the configured instance.",
            json_schema_extra={"pattern": f"^{sqlserver.DATABASE.pattern}$"},
        )] = "AxDB",
    ) -> dict[str, Any]:
        """Run SQL against the box database. The script can write. Read row sets or affected counts for each GO batch."""
        started = time.monotonic()  # `timeout` bounds the whole script: no batch starts after it
        if timeout == sqlserver.TIMEOUT_DEFAULT:  # the default fits the Pond's bound (schema unchanged)
            timeout = min(timeout, sql_max)
        if not sql.strip() or len(sql) > 1_000_000:
            raise Refused("invalid_request", "sql: one script, up to 1 MB")
        if not 1 <= timeout <= sql_max:
            raise Refused("invalid_request", f"timeout must be 1-{sql_max} seconds here")
        if not sqlserver.DATABASE.fullmatch(database):
            raise Refused("invalid_request", "database is a database name", database=database)
        result = sqlserver.execute(sql_connect(database, timeout), sql, max_rows, atomic,
                                   deadline=started + timeout)
        return {"database": database, **result}
