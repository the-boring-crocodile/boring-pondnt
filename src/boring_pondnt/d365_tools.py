"""D365 tool registration and diagnostic presentation; lifecycle belongs to the server."""
from __future__ import annotations
from typing import Callable
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from . import sqlserver
from .d365 import TAIL_LINES_DEFAULT, WAIT_SECONDS_MAX, Jobs, Session, check_tail_lines, checked_name, ps_quote
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

_SECRET_NAME = r"[a-z0-9_]*(?:password|pwd|secret|accountkey|sharedaccesskey|sig|token)"
_SECRET_KEY = re.compile(r"(?i)^" + _SECRET_NAME + r"$")
_SECRET = re.compile(
    r"(?i)\b(" + _SECRET_NAME + r")([\"']?\s*[=:]\s*|\s+)"
    r"('(?:[^']|'')*'|\"[^\"]*\"|'[^']*$|\"[^\"]*$|[^\s\"'&]+)")


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
        return _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}***", value)
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
                   state: Path, sql_connect: Callable[..., Any], call_budget: float | None = None) -> None:
    """`call_budget` (behind a Pond, ServiceConfig.call_limit): no call may outlast it."""
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
    def d365_build(module: str, include_reports: bool = False, dry_run: bool = False) -> dict[str, Any]:
        """Build a package (X++ compile and labels) as a job; dry_run shows the command only."""
        name = checked_name("module", module)
        logs = state / "d365-logs" / name
        if not dry_run:
            logs.mkdir(parents=True, exist_ok=True)
        script = f"Invoke-D365ModuleBuild -Module {ps_quote(name)} -LogPath {ps_quote(str(logs))}"
        if include_reports:
            script += " -IncludeReports"
        return act("build", script, dry_run)

    @tool(READ_ONLY, "reader")
    def d365_results(module: str | None = None, kind: str = "compile", warnings: bool | None = None,
                     limit: int = COMPILER_ROWS_DEFAULT) -> dict[str, Any]:
        """Last compile, best_practice (of a module) or sync: totals, then errors (and warnings) as rows."""
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
    def d365_db_sync(modules: list[str] | None = None, dry_run: bool = False) -> dict[str, Any]:
        """Synchronize the database as a job (given packages, or all); then d365_results kind=sync."""
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
    def d365_best_practice(module: str, model: str, dry_run: bool = False) -> dict[str, Any]:
        """Run best practice checks on a model as a job; dry_run shows the command only."""
        name = checked_name("module", module)
        logs = state / "d365-logs" / name
        if not dry_run:
            logs.mkdir(parents=True, exist_ok=True)
        script = (f"Invoke-D365BestPractice -Module {ps_quote(name)} -Model {ps_quote(checked_name('model', model))} "
                  f"-LogPath {ps_quote(str(logs))}")
        return act("best_practice", script, dry_run)

    @tool(RUNS, "writer")
    def d365_run(command: str, timeout: int = min(RUN_TIMEOUT_DEFAULT, run_max), background: bool = False,
                 max_chars: int = RUN_OUTPUT_DEFAULT) -> dict[str, Any]:
        """Run d365fo.tools; timeout bounds foreground only (default up to 60 s); background returns a job. Credentials masked."""
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

    @tool(WRITES, "writer")
    def d365_job(job_id: str | None = None, tail_lines: int = TAIL_LINES_DEFAULT, stop: bool = False,
                 wait_seconds: float = 0) -> dict[str, Any]:
        """Job state/tail; wait_seconds 0-60 observes, stop=true requests termination. No id: list. Stop may change state."""
        if not 0 <= wait_seconds <= wait_max:
            raise Refused("invalid_request", f"wait_seconds must be 0-{wait_max:g} here")
        if job_id is None:
            if stop or wait_seconds != 0:
                raise Refused("invalid_request", "stop/wait requires job_id")
            return {"jobs": jobs.list()}
        if stop and wait_seconds != 0:
            raise Refused("invalid_request", "choose stop or wait, not both")
        check_tail_lines(tail_lines)  # refuse invalid output options before stop can have effects
        status = jobs.stop(job_id) if stop else jobs.wait(job_id, wait_seconds)
        return {**status, "tail": jobs.tail(job_id, tail_lines)}

    @tool(WRITES, "writer")
    def d365_sql(sql: str, max_rows: int = sqlserver.ROWS_DEFAULT, timeout: int = sqlserver.TIMEOUT_DEFAULT,
                 atomic: bool = False, database: str = "AxDB") -> dict[str, Any]:
        """SQL on this box's database (GO batches): rows as JSON. Meant for test setup and checks."""
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
