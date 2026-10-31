"""The MCP layer: tool definitions only. Logic lives in the other modules.

Served over HTTP from a configuration (`--config`, boring-service-kit's
ServiceConfig): for Copilot on loopback, started at logon as the person,
or behind a Pond through the tunnel. Stdio is gone. Descriptions are short on
purpose: every definition is re-sent with every Copilot request.
Each tool is registered twice: as an MCP tool (compact result, outcomes as
`isError`) and as a plain function that `batch` calls. Both are the same
permission-checked function (boring-service-kit's `permitted`): every tool
names the role it needs, reader for reads, writer for writes, execution and
job control. `batch` itself needs reader; its preflight checks every step's
role before any step runs, so a reader's batch with one writer step is
refused whole, like any other invalid step.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from contextlib import asynccontextmanager

import anyio
from pathlib import Path
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import ValidationError

from . import __version__, binfiles, disk, execs, jobs, listing, textfiles
from .core_file_tools import register_file_tools
from boring_service_kit.access import permitted, require
from boring_service_kit.calllog import CallLog
from boring_service_kit.http import ServiceConfig, add_config_argument
from boring_service_kit.pond import attach_heartbeat
from boring_service_kit.tools import READ_ONLY, RUNS, WRITES, as_mcp_tool, default_state_dir, refuse_unknown_arguments
from boring_service_kit.outcomes import Failed, Outcome, Refused

INSTRUCTIONS = (
    "Local tools for this Windows development machine: read and search files anywhere, run "
    "commands and long jobs, and verify edits (encodings and line endings are kept, output is "
    "bounded). Everything Visual Studio manages stays with VS/GitHub Copilot tooling, including "
    "creation, edits, deletion, builds and generated-artifact cleanup; no external workaround "
    "after an IDE failure. Use these tools within the agreed scope outside that ownership. "
    "Call `bootstrap` at session start and after context loss. Paths are absolute "
    "Windows paths; edits name the sha256 of the latest read."
)

BATCH_MAX_STEPS = 32
BATCH_TIMEOUT_DEFAULT = 300.0
BATCH_TIMEOUT_MAX = 600.0
BATCH_RESULT_MAX = 256 * 1024  # characters of all step results together (initial)
NOT_IN_BATCH = {"bootstrap", "batch"}


def long_paths_enabled() -> bool | None:
    """True or False on Windows; None elsewhere."""
    if sys.platform != "win32":
        return None
    import winreg  # noqa: PLC0415 -- Windows only

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
            return winreg.QueryValueEx(key, "LongPathsEnabled")[0] == 1
    except OSError:
        return False


def bootstrap_result(kit: Path | None) -> dict[str, Any]:
    result: dict[str, Any] = {"server": "boring-pondnt-core", "version": __version__}
    if kit is None:
        result["handbook"] = None
        result["note"] = "no handbook configured (--handbook); working without the shared agreement"
    else:
        start = kit / "bootstrap.md"
        try:
            result["bootstrap"] = start.read_text(encoding="utf-8-sig")
        except OSError as exc:
            result["bootstrap"] = None
            result["note"] = f"handbook bootstrap not readable: {exc}"
        result["handbook"] = str(kit)
        result["handbook_files"] = sorted(str(p) for p in kit.rglob("*.md") if p != start)
        shipped = default_kit()
        if shipped is not None and shipped.resolve() != kit.resolve():
            # The served kit is the owner's; the release's own kit is only a proposal.
            result["release_handbook"] = str(shipped)
    checks: dict[str, Any] = {}
    if sys.platform == "win32":
        checks["default_shell"] = execs.default_shell()
        checks["pwsh"] = execs.pwsh_path()
        if checks["pwsh"] is None:
            checks["shell_note"] = "PowerShell 7 not found; exec falls back to Windows PowerShell 5.1"
    lp = long_paths_enabled()
    if lp is not None:
        checks["long_paths_enabled"] = lp
        if not lp:
            checks["warning"] = "LongPathsEnabled is off; deep paths will fail (an admin enables it once)"
    if checks:
        result["checks"] = checks
    return result


def default_kit() -> Path | None:
    """The kit shipped in the release folder (`<release>/handbook`), if any."""
    bundled = Path(sys.prefix).parent / "handbook"
    return bundled if (bundled / "bootstrap.md").is_file() else None


def create_server(kit: Path | None = None, state_dir: Path | None = None,
                  log_calls: bool = False, token_verifier: Any = None, auth: Any = None,
                  on_discover: Callable[[str], None] | None = None,
                  call_budget: float | None = None, auth_server_provider: Any = None) -> MCPServer:
    """The tools; over HTTP also the token check and the discover hook (boring-service-kit).

    `call_budget` (behind a Pond: every call must answer within the Pond's call
    bound, ServiceConfig.call_limit) caps exec, job_wait and batch at that many
    seconds and makes those the defaults. The descriptions say so, since a Pond
    agent sees only them. None (stdio, or HTTP for Copilot on loopback): no cap.

    A server that authenticates callers (any ServiceConfig: issuer, oauth or keys)
    authorizes every call by the caller's role. Without authentication (in-process
    use, tests) there is no caller to authorize.
    """
    budget = call_budget
    enforce_access = token_verifier is not None or auth_server_provider is not None

    def within_budget(name: str, seconds: float) -> None:
        if budget is not None and seconds > budget:
            raise Refused("invalid_request",
                          f"{name} must be at most {budget:g} s here: every call answers within {budget:g} s "
                          "(a Pond service); longer work: job_start, then job_wait in short steps")

    def described(text: str) -> Callable[[Any], Any]:
        def set_doc(fn: Any) -> Any:
            fn.__doc__ = text
            return fn
        return set_doc

    def default_shell(shell: str | None) -> str | None:
        return shell  # execs picks PowerShell 7 when bundled, as over stdio

    exec_default = min(execs.TIMEOUT_DEFAULT, budget) if budget else execs.TIMEOUT_DEFAULT
    wait_default = min(30.0, budget) if budget else 30.0
    batch_default = min(BATCH_TIMEOUT_DEFAULT, budget) if budget else BATCH_TIMEOUT_DEFAULT
    shells = "default PowerShell 7; powershell=5.1, cmd"
    limit = f"<={budget:g} s here; longer: job_start" if budget else "<=600 s"
    job_manager: list[jobs.Jobs] = []  # discovery does not create state
    manager_lock = threading.Lock()
    closed = False

    def manager() -> jobs.Jobs:
        with manager_lock:
            if closed:
                raise Failed("unavailable", "the server is closed")
            if not job_manager:
                job_manager.append(jobs.Jobs(state_dir or default_state_dir("boring-pondnt")))
            return job_manager[0]

    def shutdown() -> None:
        nonlocal closed
        with manager_lock:
            closed = True
            current = job_manager[0] if job_manager else None
        if current is not None:
            current.shutdown()

    @asynccontextmanager
    async def lifespan(server: Any):
        try:
            yield
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(shutdown)

    mcp = MCPServer("boring-pondnt-core", instructions=INSTRUCTIONS, version=__version__,
                    token_verifier=token_verifier, auth=auth, auth_server_provider=auth_server_provider,
                    lifespan=lifespan)
    attach_heartbeat(mcp, on_discover)
    log = CallLog((state_dir or default_state_dir("boring-pondnt")) / "calls.jsonl" if log_calls else None)
    plain: dict[str, Callable[..., dict[str, Any]]] = {}
    roles: dict[str, str] = {}
    def check_arguments(method: str, args: dict[str, Any]) -> None:
        """Use operation owners' stateless checks; never create managers or access file contents."""
        if enforce_access:
            require(roles[method])  # the step's own role, before any step runs
        if method.startswith("file_"):
            disk.absolute(args["path"])
        if method == "file_read":
            textfiles.check_read(args["start_line"], args["line_count"])
        elif method == "file_write":
            textfiles.encode(args["content"], textfiles.target_format(
                None, args["encoding"], args["bom"], args["newline"]))
        elif method == "file_patch":
            fmt = textfiles.target_format(None, None, None, args["newline"])
            for _, replacement in textfiles.check_patch(args["edits"]):
                textfiles.encode(replacement, fmt)
        elif method == "file_list":
            listing.check_list(args["limit"])
        elif method == "file_search":
            listing.check_search(args["query"], args["limit"])
        elif method == "file_read_bytes":
            binfiles.check_read(args["offset"], args["length"])
        elif method == "file_write_bytes":
            binfiles.decode_write(args["data"], args["expected_sha256"], args["offset"])
        elif method in ("exec", "job_start"):
            if method == "exec":
                within_budget("timeout_seconds", args["timeout_seconds"])
            maximum = execs.TIMEOUT_MAX if method == "exec" else jobs.TIMEOUT_MAX
            execs.check_command(args["command"], default_shell(args["shell"]), args["timeout_seconds"], maximum)
            if args["cwd"] is not None:
                disk.absolute(args["cwd"])
        elif method == "job_wait":
            within_budget("wait_seconds", args["wait_seconds"])
            jobs.check_wait(args["job_ids"], args["wait_seconds"])
        elif method == "job_output":
            jobs.check_output(args["stream"], args["tail_lines"], args["start_line"], args["line_count"])
        elif method == "job_list":
            jobs.check_list(args["limit"])

    def tool(annotations: ToolAnnotations, role: str) -> Callable[[Callable[..., dict[str, Any]]], None]:
        """Register `fn` for MCP and for batch, both behind the same permission check."""
        def register(fn: Callable[..., dict[str, Any]]) -> None:
            guarded = permitted(fn, role) if enforce_access else fn
            plain[fn.__name__] = guarded
            roles[fn.__name__] = role
            mcp.tool(annotations=annotations)(as_mcp_tool(guarded, log))
        return register

    @tool(READ_ONLY, "reader")
    def bootstrap() -> dict[str, Any]:
        """Start here once per session: the working agreement and what this machine offers."""
        return bootstrap_result(kit)

    register_file_tools(tool)

    @tool(RUNS, "writer")
    @described(f"Run a command ({shells}); exit code, bounded output. {limit}.")
    def exec(command: str, shell: str | None = None, cwd: str | None = None,
             timeout_seconds: float = exec_default) -> dict[str, Any]:
        within_budget("timeout_seconds", timeout_seconds)
        return execs.run(command, default_shell(shell), cwd, timeout_seconds)

    @tool(RUNS, "writer")
    def job_start(command: str, shell: str | None = None, cwd: str | None = None,
                  timeout_seconds: float = 3600.0) -> dict[str, Any]:
        """Start a long command (build, sync, tests) in the background; returns job_id. <=24 h."""
        return manager().start(command, default_shell(shell), cwd, timeout_seconds)

    @tool(READ_ONLY, "reader")
    @described("Status of jobs (state, exit code, duration, output sizes); waits up to "
               + (f"{budget:g} s here" if budget else "60 s") + " for all to end. 0 = just status.")
    def job_wait(job_ids: list[str], wait_seconds: float = wait_default) -> dict[str, Any]:
        within_budget("wait_seconds", wait_seconds)
        return manager().wait(job_ids, wait_seconds)

    @tool(READ_ONLY, "reader")
    def job_output(job_id: str, stream: str = "stdout", tail_lines: int | None = None,
                   start_line: int | None = None, line_count: int = jobs.OUTPUT_LINES_DEFAULT) -> dict[str, Any]:
        """A job's output: last tail_lines (default 100), or line_count lines from start_line."""
        return manager().output(job_id, stream, tail_lines, start_line, line_count)

    @tool(WRITES, "writer")
    def job_stop(job_id: str) -> dict[str, Any]:
        """Stop a job and everything it started."""
        return manager().stop(job_id)

    @tool(READ_ONLY, "reader")
    def job_list(limit: int = 20) -> dict[str, Any]:
        """This session's jobs, newest first."""
        return manager().list(limit)

    @tool(RUNS, "reader")  # each step needs its own role: checked in check_arguments
    @described("Several file/exec/job steps in one call, in order: {id, method, arguments}. Saves round trips."
               + (f" All within {budget:g} s here." if budget else ""))
    def batch(operations: list[dict[str, Any]], on_error: str = "stop",
              timeout_seconds: float = batch_default) -> dict[str, Any]:
        within_budget("timeout_seconds", timeout_seconds)
        return run_batch(mcp, plain, operations, on_error, timeout_seconds, check_arguments)

    refuse_unknown_arguments(mcp)
    return mcp


def exec_failure(result: dict[str, Any]) -> str:
    """Why an exec step counts as failed in a batch."""
    if result["timed_out"]:
        return "command timed out"
    if result.get("cleanup_confirmed") is False:
        return "command cleanup unconfirmed"
    if result["exit_code"] is None:
        return "command failed"
    return "command exited nonzero"


def run_batch(mcp: MCPServer, plain: dict[str, Callable[..., dict[str, Any]]],
              operations: list[dict[str, Any]], on_error: str, timeout_seconds: float,
              check_arguments: Callable[[str, dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Like the Pond's batch: every step is checked before any runs; no rollback."""
    if on_error not in ("stop", "continue"):
        raise Refused("invalid_request", "on_error must be stop or continue")
    if not 0.1 <= timeout_seconds <= BATCH_TIMEOUT_MAX:
        raise Refused("invalid_request", f"timeout_seconds must be 0.1-{BATCH_TIMEOUT_MAX:g}")
    if not 1 <= len(operations) <= BATCH_MAX_STEPS:
        raise Refused("invalid_request", f"operations must list 1-{BATCH_MAX_STEPS} steps")
    prepared: list[tuple[str, str, dict[str, Any]]] = []
    seen: set[str] = set()
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict) or set(operation) - {"id", "method", "arguments"}:
            raise Refused("invalid_request", "a step has only id, method and arguments", step=index)
        step_id, method = operation.get("id"), operation.get("method")
        if not isinstance(step_id, str) or not step_id or step_id in seen:
            raise Refused("invalid_request", "each step needs a unique, nonempty string id", step=index)
        seen.add(step_id)
        if not isinstance(method, str) or method not in plain or method in NOT_IN_BATCH:
            if isinstance(method, str) and (method.replace("-", "_").startswith(("d365_", "xref_"))
                                            or method == "xref"):
                # Seen on a dev VM: Copilot put "xref-names" into a core batch.
                raise Refused("invalid_request", f"{method} is a tool of the d365 server, not of this one: "
                              "call it directly; batch takes only this server's methods", step=step_id)
            allowed = sorted(set(plain) - NOT_IN_BATCH)
            raise Refused("invalid_request", f"unknown method; one of {', '.join(allowed)}", step=step_id)
        arguments = operation.get("arguments", {})
        model = mcp._tool_manager.get_tool(method).fn_metadata.arg_model
        try:
            validated = model.model_validate(arguments)
        except ValidationError as exc:
            problems = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
            raise Refused("invalid_request", "invalid arguments", step=step_id, problems=problems) from None
        values = validated.model_dump_one_level()
        if check_arguments is not None:
            try:
                check_arguments(method, values)
            except Refused as refusal:
                refusal.details = {**refusal.details, "step": step_id}
                raise
        prepared.append((step_id, method, values))

    started = time.monotonic()
    deadline = started + timeout_seconds
    results: list[dict[str, Any]] = []
    stop_reason = None
    result_chars = 0
    for step_id, method, arguments in prepared:
        if stop_reason is not None:
            results.append({"id": step_id, "method": method, "status": "not_started"})
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0 or (method == "exec" and remaining < 0.1):
            stop_reason = "timeout"
            results.append({"id": step_id, "method": method, "status": "not_started"})
            continue
        # Do not turn an invalid request into a valid one while narrowing its budget.
        # The operation owner retains validation and cleanup responsibility.
        if method == "exec" and 0.1 <= arguments["timeout_seconds"] <= execs.TIMEOUT_MAX:
            arguments["timeout_seconds"] = min(arguments["timeout_seconds"], remaining)
        elif method == "job_wait" and 0 <= arguments["wait_seconds"] <= jobs.WAIT_MAX:
            arguments["wait_seconds"] = min(arguments["wait_seconds"], remaining)
        step_start = time.monotonic()
        entry: dict[str, Any] = {"id": step_id, "method": method}
        try:
            entry["status"] = "succeeded"
            result = entry["result"] = plain[method](**arguments)
            if method == "exec" and (result["timed_out"] or result["exit_code"] != 0):
                entry["status"] = "failed"
                entry["failed"] = {"status": "failed", "code": "command_failed",
                                   "message": exec_failure(result)}
        except Outcome as outcome:
            entry["status"] = outcome.status
            entry[outcome.status if outcome.status == "failed" else "refusal"] = outcome.as_dict()
        except Exception as exc:  # a bug, not an outcome: report it, keep the batch honest
            entry["status"] = "failed"
            entry["failed"] = {"status": "failed", "code": "internal_error", "message": repr(exc)}
        entry["duration_ms"] = round((time.monotonic() - step_start) * 1000)
        results.append(entry)
        result_chars += len(json.dumps(entry, ensure_ascii=False, separators=(",", ":")))
        if entry["status"] != "succeeded" and on_error == "stop":
            stop_reason = f"step_{entry['status']}"
        elif result_chars > BATCH_RESULT_MAX:
            stop_reason = "result_limit"  # every result is re-sent on every later turn
    return {
        "results": results,
        "stop_reason": stop_reason,
        "all_attempted": all(r["status"] != "not_started" for r in results),
        "result_chars": result_chars,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def main(argv: list[str] | None = None) -> None:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="boring-pondnt-core", description=INSTRUCTIONS)
    parser.add_argument("--handbook", dest="kit", type=Path, help="handbook folder with bootstrap.md")
    parser.add_argument("--state-dir", type=Path, help=f"job output, call log (default {default_state_dir('boring-pondnt')})")
    parser.add_argument("--no-call-log", action="store_true", help="do not write calls.jsonl")
    parser.add_argument("--self-check", action="store_true", help="check this machine, print JSON, exit")
    http = parser.add_argument_group("HTTP (the only transport; setup.ps1 writes the configuration)")
    add_config_argument(http)
    http.add_argument("--call-budget", type=float, default=None,
                      help="seconds every call must answer within; caps exec, job_wait, batch "
                           "(default: the Pond's call bound with a pond section, none without)")
    args = parser.parse_args(argv)
    if args.self_check:
        from .selfcheck import main as self_check  # noqa: PLC0415

        raise SystemExit(self_check(args.state_dir))
    if args.config:
        # Who may call is the configuration's access list; tokens come from its issuer.
        config = ServiceConfig(args.config)
        budget = args.call_budget if args.call_budget is not None else config.call_limit
        config.serve(lambda auth, on_discover: create_server(
            args.kit or default_kit(), args.state_dir, log_calls=not args.no_call_log, on_discover=on_discover,
            call_budget=budget, **auth))
        return
    parser.error("--config is required: the core speaks HTTP only; setup.ps1 writes the configuration")


if __name__ == "__main__":
    main()
