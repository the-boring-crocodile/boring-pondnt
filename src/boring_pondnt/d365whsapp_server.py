"""boring-pondnt-d365whsapp: drive the Warehouse Management mobile app protocol for automated tests.

    boring-pondnt-d365whsapp serve --config <service.json> --d365whsapp <d365whsapp.json> [--state-dir <dir>]

d365whsapp.json, mode app (the default): {"aos_url": "...", "tenant": "...", "client_id": "...",
"secret_file": "C:\\ProgramData\\d365whsapp\\client-secret.dpapi", "company": "<optional default company>"}.
The secret file holds the Entra client secret, DPAPI-protected for the account
the server runs as. One worker = one server session: parallel runs use
different workers.

Mode form (D38, never the default): {"mode": "form", "form": {"url": "https://<aos host>", "company": "...",
"profile": "d365whsapp-form", "debug_port": 9334}}: the in-form WHS app simulator in a signed-in Edge
of its own (the form block takes d365web.json's keys; docs/D365WHSAPP.md). Same tools, same steps;
every result carries `mode`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__
from .collect import Run, Runs, StillRunning
from .scenario_values import substitute
from .d365whsapp_backends import AppBackend, FormBackend, load_backend
from .d365whsapp_client import WhsClient, WhsError
from .d365whsapp_steplog import StepLog
from .whs_steps import StepError, check, check_steps
from .tool_schema import AnyMap, TextMap
from boring_service_kit.access import permitted
from boring_service_kit.calllog import CallLog
from boring_service_kit.http import ServiceConfig, add_config_argument
from boring_service_kit.outcomes import Failed, Outcome, Refused
from boring_service_kit.pond import attach_heartbeat
from boring_service_kit.tools import READ_ONLY, RUNS, as_mcp_tool, default_state_dir, refuse_unknown_arguments

INSTRUCTIONS = (
    "Emulates the D365 Warehouse Management mobile app for automated tests. whs_login a test worker "
    "(one worker per parallel run), then whs_submit: fill enabled fields and press one button; the "
    "result is the next page. whs_run_scenario runs a list of steps with expectations. Each worker "
    "has one server session; steps change real data in the connected D365."
)  # + the mode's own sentence (create_server)
SCENARIO_STEPS_MAX = 50
REFUSAL_CODES = {"refused", "no_session", "no_company", "busy", "no_run", "not_found", "covered"}
BUDGET_MARGIN = 5.0  # covers the recovery login (well under a second when warm)


def _outcome(error: WhsError) -> Outcome:
    details = {"page": error.page.summary()} if error.page is not None else {}
    details.update(getattr(error, "details", None) or {})  # form mode: d365web's details (its page)
    cls = Refused if error.code in REFUSAL_CODES else Failed
    return cls(error.code, error.message, **details)


class ScenarioRuns:
    """One background whs_run_scenario per worker, kept until whs_wait collects it (collect.Runs).

    A Pond call ends after its deadline (30 s); a scenario with bookings can run for minutes. The run
    goes on in a thread; the call answers `running` with the steps done so far, whs_wait collects.
    While a run is open (running or finished but not collected) every acting call for that worker is
    refused as busy, so a result is never lost or interleaved with other steps; while an acting call
    runs (`acting`), a scenario for that worker is refused as busy (check and reservation under the
    runs' one lock)."""

    def __init__(self, budget: float | None) -> None:
        self.budget = budget  # None: answer only when finished (no deadline to respect)
        self.runs = Runs(ThreadPoolExecutor(max_workers=8, thread_name_prefix="whs-scenario"))
        self.active: dict[str, int] = {}  # worker -> acting calls in progress

    @staticmethod
    def _open(key: str) -> Refused:
        return Refused("busy", f"worker {key} has a scenario run open; collect it with whs_wait")

    @contextmanager
    def acting(self, worker: str) -> Iterator[None]:
        """Reserve the worker for one acting call (login, submit, logoff): refused while a
        scenario run is open; a scenario cannot start until the call is done."""
        key = worker.upper()
        with self.runs.lock:
            if self.runs.peek(key) is not None:
                raise self._open(key)
            self.active[key] = self.active.get(key, 0) + 1
        try:
            yield
        finally:
            with self.runs.lock:
                self.active[key] -= 1
                if not self.active[key]:
                    del self.active[key]

    def start(self, worker: str, run: Any) -> dict[str, Any]:
        key = worker.upper()
        transcript: list[dict[str, Any]] = []

        def admit() -> None:
            if self.active.get(key):
                raise Refused("busy", f"worker {key} has a step in progress; start the scenario after it")

        entry = self.runs.start(key, "whs_run_scenario", run, transcript, owner=transcript,
                                refuse=lambda _: self._open(key), admit=admit)
        return self._answer(key, entry, self.budget)

    def wait(self, worker: str, seconds: float | None) -> dict[str, Any]:
        key = worker.upper()
        entry = self.runs.get(key)
        if entry is None:
            raise Refused("no_run", f"worker {key} has no scenario run to collect")
        limit = self.budget if seconds is None else (min(seconds, self.budget) if self.budget else seconds)
        return self._answer(key, entry, limit)

    def _answer(self, key: str, entry: Run, limit: float | None) -> dict[str, Any]:
        try:
            return self.runs.collect(key, entry, limit)  # a WhsError/Outcome raised by the run surfaces here
        except StillRunning:
            transcript = entry.owner
            return {"status": "running", "worker": key, "steps_done": len(transcript),
                    "last": transcript[-1] if transcript else None,
                    "hint": "the scenario is still running in D365; call whs_wait for its result"}


def _step_action(backend: AppBackend | FormBackend, worker: str, values: dict[str, Any] | None,
                 press: str | None) -> str:
    """'press OK, values {Item [ItemId]: A0001}' (password-type fields masked)."""
    parts = []
    if press:
        parts.append(f"press {press}")
    shown = backend.describe_values(worker, values)
    if shown:
        parts.append(f"values {shown}")
    return ", ".join(parts) or "submit (nothing set, no button)"


def create_server(backend: AppBackend | FormBackend | WhsClient, state_dir: Path | None = None,
                  log_calls: bool = False, token_verifier: Any = None, auth: Any = None, on_discover: Any = None,
                  auth_server_provider: Any = None, call_budget: float | None = None) -> MCPServer:
    """Reader: whs_screen, whs_log. Writer: everything that talks to D365."""
    if isinstance(backend, WhsClient):
        backend = AppBackend(backend)
    enforce_access = token_verifier is not None or auth_server_provider is not None
    if call_budget:
        backend.limit(call_budget - BUDGET_MARGIN)
    state = state_dir or default_state_dir("boring-pondnt")
    mcp = MCPServer("boring-pondnt-d365whsapp", instructions=f"{INSTRUCTIONS} {backend.describe}",
                    version=__version__, token_verifier=token_verifier, auth=auth,
                    auth_server_provider=auth_server_provider)
    attach_heartbeat(mcp, on_discover)
    log = CallLog(state / "calls-d365whsapp.jsonl" if log_calls else None)
    runs = ScenarioRuns(call_budget - BUDGET_MARGIN if call_budget else None)
    steps_log = StepLog(state / "whs-steps")
    mode = backend.mode

    def note(result: dict[str, Any], worker: str, action: str, shown: Any = None, **extra: Any) -> dict[str, Any]:
        problem = steps_log.record(worker, action, lines=shown.lines if shown is not None else None, **extra)
        if problem:
            result["log_error"] = problem
        return result

    def act(worker: str, action: str, call: Any) -> Any:
        """Run one acting call; a WhsError is logged (with its page, if any) and raised again."""
        try:
            return call()
        except WhsError as error:
            steps_log.record(worker, action, error=f"{error.code}: {error.message}",
                             lines=backend.error_lines(error))
            raise

    def tool(annotations: Any, role: str) -> Any:
        def register(fn: Any) -> Any:
            def safe(*args: Any, **kwargs: Any) -> dict[str, Any]:
                try:
                    result = fn(*args, **kwargs)
                except WhsError as error:
                    raise _outcome(error) from None
                return {"mode": mode, **result}  # every answer says which backend gave it (D38)
            safe.__name__, safe.__doc__, safe.__wrapped__ = fn.__name__, fn.__doc__, fn  # type: ignore[attr-defined]
            guarded = permitted(safe, role) if enforce_access else safe
            mcp.tool(annotations=annotations)(as_mcp_tool(guarded, log))
            return fn
        return register

    @tool(RUNS, "writer")
    def whs_login(worker: str, password: str, company: str | None = None) -> dict[str, Any]:
        """Log a test worker in (company: this argument, else the config's; there is no built-in one). If the worker already has a server
        session, it resumes at its last page (resumed=true). A failed login is never retried."""
        action = "login" + (f" company {company}" if company else "") + " (password not logged)"
        with runs.acting(worker):
            shown, resumed = act(worker, action, lambda: backend.login(worker, password, company))
        return note({"worker": worker.upper(), "resumed": resumed, **shown.summary}, worker,
                    action + (" (resumed)" if resumed else ""), shown)

    @tool(READ_ONLY, "reader")
    def whs_screen(worker: str) -> dict[str, Any]:
        """The worker's current page; form mode first collects that worker's outstanding step result or error."""
        shown = act(worker, "screen", lambda: backend.screen(worker))
        return {"worker": worker.upper(), **shown.summary}

    @tool(RUNS, "writer")
    def whs_submit(worker: str, values: TextMap = None, press: str | None = None) -> dict[str, Any]:
        """Fill enabled fields (name -> text) and press a button (name); returns the next page.
        recovered=true: the reply was lost and the session was resumed by a new login; check the
        page (the step may or may not have run). Values for read-only fields are refused."""
        action = _step_action(backend, worker, values, press)
        with runs.acting(worker):
            shown, recovered = act(worker, action, lambda: backend.submit(worker, values, press))
        result = {"worker": worker.upper(), **shown.summary}
        if recovered:
            result["recovered"] = True
        return note(result, worker, action, shown, recovered=recovered)

    @tool(RUNS, "writer")
    def whs_logoff(worker: str) -> dict[str, Any]:
        """Cancel/Back/Leave to the main menu and log off; ends the worker's session."""
        with runs.acting(worker):
            shown = act(worker, "logoff", lambda: backend.logoff(worker))
        logged_off = shown is None or shown.view.page == "Login"
        return note({"worker": worker.upper(), "logged_off": logged_off}, worker, "logoff", shown)

    @tool(RUNS, "writer")
    def whs_run_scenario(worker: str, steps: list[dict[str, Any]], stop_on_mismatch: bool = True,
                         variables: AnyMap = None) -> dict[str, Any]:
        """Run steps on a logged-in worker. Step: {"values": {...}, "press": "...", "expect":
        {"page", "step", "title", "message_kind", "message" (substring), "fields": {name: data},
        "labels": {name: substring of the label}}, "note": "..."}.
        ${name} in values, press and expect is replaced from variables (e.g. a PO number that
        d365web's web_run_scenario saved). Returns one line per step and the final page; ok=false
        at the first mismatch or error. Mode form lists what it cannot check in not_checked. A run
        that outlasts the call answers status=running with steps_done; whs_wait collects the result
        (the worker is busy until then)."""
        steps = substitute(steps, {k: str(v) for k, v in (variables or {}).items()},
                           lambda message: Refused("refused", message))
        try:
            check_steps(steps)
        except StepError as error:
            raise Refused("refused", str(error)) from None
        if len(steps) > SCENARIO_STEPS_MAX:
            raise Refused("refused", f"at most {SCENARIO_STEPS_MAX} steps per call")
        backend.require_session(worker)  # refuse before any step when not logged in
        return runs.start(worker, lambda transcript: _run(worker, steps, stop_on_mismatch, transcript))

    def _run(worker: str, steps: list[dict[str, Any]], stop_on_mismatch: bool,
             transcript: list[dict[str, Any]]) -> dict[str, Any]:
        shown = None
        ok = True
        unseen: set[str] = set()
        for index, step in enumerate(steps, 1):
            line: dict[str, Any] = {"step": index}
            if step.get("note"):
                line["note"] = step["note"]
            action = f"scenario step {index}: " + _step_action(backend, worker, step.get("values"), step.get("press"))
            try:
                shown, recovered = backend.submit(worker, step.get("values"), step.get("press"), in_run=True)
            except WhsError as error:
                line.update(error=error.code, message=error.message)
                if getattr(error, "details", None):
                    line["details"] = error.details
                problem = steps_log.record(worker, action, error=f"{error.code}: {error.message}",
                                           lines=backend.error_lines(error))
                if problem:
                    line["log_error"] = problem
                transcript.append(line)
                ok = False
                break
            line.update(shown.line)
            if recovered:
                line["recovered"] = True
            misses, not_seen = check(shown.view, step.get("expect") or {})
            unseen.update(not_seen)
            if misses:
                line["mismatch"] = misses
                ok = False
            problem = steps_log.record(worker, action + (f"  (mismatch: {misses})" if misses else ""),
                                       lines=shown.lines, recovered=recovered)
            if problem:
                line["log_error"] = problem
            transcript.append(line)
            if misses and stop_on_mismatch:
                break
        result: dict[str, Any] = {"mode": mode, "worker": worker.upper(), "ok": ok, "steps": list(transcript)}
        if unseen:
            result["not_checked"] = sorted(unseen)
        if shown is not None:
            result["final"] = shown.summary
        return result

    @tool(READ_ONLY, "reader")
    def whs_log(worker: str, lines: int = 60) -> dict[str, Any]:
        """The worker's readable step log, newest last: each login, submit, logoff and scenario step
        with the values set, the button pressed and the page D365 answered (passwords never logged).
        lines: 1-500 (at most 64 KiB)."""
        if not 1 <= lines <= 500:
            raise Refused("refused", "lines must be 1-500")
        text = steps_log.tail(worker, lines)
        if text is None:
            raise Refused("not_found", f"worker {worker.upper()} has no step log yet")
        return {"worker": worker.upper(), "log": text, "file": str(steps_log.path(worker))}

    @tool(RUNS, "writer")
    def whs_wait(worker: str, seconds: float | None = None) -> dict[str, Any]:
        """The result of a whs_run_scenario that answered status=running (again running, with
        steps_done, if it still is). seconds caps the wait (default: the call budget)."""
        return runs.wait(worker, seconds)

    refuse_unknown_arguments(mcp)
    mcp._d365whsapp_backend = backend  # type: ignore[attr-defined]
    if isinstance(backend, AppBackend):
        mcp._d365whsapp_client = backend.client  # type: ignore[attr-defined]
    else:
        backend.web.state_dir = state  # its screenshots and page dumps next to the server's state
    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="boring-pondnt-d365whsapp", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="answer over HTTP, configured by --config (no stdio)")
    serve.add_argument("--d365whsapp", type=Path, required=True,
                       help="d365whsapp.json: mode app (AOS URL, tenant, client id, secret file) or form")
    serve.add_argument("--state-dir", type=Path)
    serve.add_argument("--no-call-log", action="store_true")
    add_config_argument(serve)
    args = parser.parse_args(argv)
    try:
        backend = load_backend(args.d365whsapp)
        if not args.config:
            parser.error("serve requires --config: the d365whsapp server speaks HTTP only")
        config = ServiceConfig(args.config)
        def make_server(auth: Any = None, on_discover: Any = None) -> MCPServer:
            return create_server(backend, args.state_dir, not args.no_call_log, on_discover=on_discover,
                                 call_budget=config.call_limit, **(auth or {}))
        config.serve(make_server)
    except Outcome as outcome:
        print(json.dumps(outcome.as_dict()), file=sys.stderr)
        raise SystemExit(2) from None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "failed", "code": "config", "message": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
