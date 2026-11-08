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
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Any
from pydantic import Field

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
REFUSAL_CODES = {"refused", "no_session", "no_company", "busy", "no_run", "not_found", "covered", "retry_later",
                 "no_lookup"}  # no_lookup: a refusal in d365web too
BUDGET_MARGIN = 5.0  # covers the recovery login (well under a second when warm)


# Each server its own name: support bundles collect several state folders flat.
CALL_LOG = "calls-d365whsapp.jsonl"


def _outcome(error: WhsError) -> Outcome:
    details = {"page": error.page.summary()} if error.page is not None else {}
    details.update(getattr(error, "details", None) or {})  # form mode: d365web's details (its page)
    cls = Refused if error.code in REFUSAL_CODES else Failed
    return cls(error.code, error.message, **details)


def worker_key(worker: str) -> str:
    """One spelling of a worker everywhere ('w1 ' and 'W1' are the same worker, as in the backends)."""
    return worker.strip().upper()


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
        key = worker_key(worker)
        with self.runs.lock:
            if self.runs.peek(key) is not None:
                raise self._open(key)
            if self.active.get(key):
                # a second step would act on a page its caller never saw (a retry of a lost reply, two
                # clients): refuse; whs_screen shows the page the first one leaves
                raise Refused("busy", f"worker {key} has a step in progress; call whs_screen once it is done "
                              "and decide on that page")
            self.active[key] = self.active.get(key, 0) + 1
        try:
            yield
        finally:
            with self.runs.lock:
                self.active[key] -= 1
                if not self.active[key]:
                    del self.active[key]

    def start(self, worker: str, run: Any) -> dict[str, Any]:
        key = worker_key(worker)
        transcript: list[dict[str, Any]] = []

        def admit() -> None:
            if self.active.get(key):
                raise Refused("busy", f"worker {key} has a step in progress; start the scenario after it")

        entry = self.runs.start(key, "whs_run_scenario", run, transcript, owner=transcript,
                                refuse=lambda _: self._open(key), admit=admit)
        return self._answer(key, entry, self.budget)

    def wait(self, worker: str, seconds: float | None) -> dict[str, Any]:
        key = worker_key(worker)
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
    log = CallLog(state / CALL_LOG if log_calls else None)
    runs = ScenarioRuns(call_budget - BUDGET_MARGIN if call_budget else None)
    steps_log = StepLog(state / "whs-steps")
    logoff_items = backend.client.logoff_items if isinstance(backend, AppBackend) else backend.logoff_items
    log_problems: dict[str, str] = {}
    log_problems_lock = threading.Lock()

    def lookup_note(worker: str, message: str) -> None:
        # A background write has no tool result; report its first failure with the next action.
        with log_problems_lock:
            problem = steps_log.record(worker, "logoff discovery", lines=[message])
            if problem:
                log_problems.setdefault(worker_key(worker), problem)

    logoff_items.observe(lookup_note)
    mode = backend.mode

    def note(result: dict[str, Any], worker: str, action: str, shown: Any = None, **extra: Any) -> dict[str, Any]:
        with log_problems_lock:
            problem = steps_log.record(worker, action, lines=shown.lines if shown is not None else None, **extra)
            problem = log_problems.pop(worker_key(worker), None) or problem
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
        finally:
            for message in backend.logoff_notes(worker):
                steps_log.record(worker, "logoff discovery", lines=[message])

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
    def whs_login(
        worker: Annotated[str, Field(description="Warehouse worker ID, not the Windows or Entra account.")],
        password: Annotated[str, Field(description="Warehouse worker password. Keep it out of notes and logs.")],
        company: Annotated[str | None, Field(description="Legal entity. Omit to use the configured company.")] = None,
    ) -> dict[str, Any]:
        """Log in a test worker or resume its current page. Check whs_screen before retrying an unfinished form step."""
        action = "login" + (f" company {company}" if company else "") + " (password not logged)"
        with runs.acting(worker):
            shown, resumed = act(worker, action, lambda: backend.login(worker, password, company))
        return note({"worker": worker.upper(), "resumed": resumed, **shown.summary}, worker,
                    action + (" (resumed)" if resumed else ""), shown)

    @tool(READ_ONLY, "reader")
    def whs_screen(
        worker: Annotated[str, Field(description="Test warehouse worker ID used with whs_login.")],
    ) -> dict[str, Any]:
        """Read a worker's current page. In form mode, also collect the result of its pending form step."""
        shown = act(worker, "screen", lambda: backend.screen(worker))
        return {"worker": worker.upper(), **shown.summary}

    @tool(RUNS, "writer")
    def whs_submit(
        worker: Annotated[str, Field(description="Test warehouse worker ID used with whs_login.")],
        values: Annotated[TextMap, Field(
            description="Map of enabled field names from whs_screen to text values.",
        )] = None,
        press: Annotated[str | None, Field(
            description="Enabled button name from whs_screen. Omit to set fields only.",
        )] = None,
    ) -> dict[str, Any]:
        """Fill fields and press a button. Use whs_screen for a pending form step. If recovered, verify before retrying."""
        action = _step_action(backend, worker, values, press)
        with runs.acting(worker):
            shown, recovered = act(worker, action, lambda: backend.submit(worker, values, press))
        result = {"worker": worker.upper(), **shown.summary}
        if recovered:
            result["recovered"] = True
        return note(result, worker, action, shown, recovered=recovered)

    @tool(RUNS, "writer")
    def whs_logoff(
        worker: Annotated[str, Field(description="Test warehouse worker ID used with whs_login.")],
    ) -> dict[str, Any]:
        """Leave the worker's pages and end its warehouse session. retry_later: the logoff names are still
        being looked up (right after a D365 start); nothing was pressed, the session is kept; call again in a few seconds."""
        with runs.acting(worker):
            shown = act(worker, "logoff", lambda: backend.logoff(worker))
        logged_off = shown is None or shown.view.page == "Login"
        return note({"worker": worker.upper(), "logged_off": logged_off}, worker, "logoff", shown)

    @tool(RUNS, "writer")
    def whs_run_scenario(
        worker: Annotated[str, Field(description="Test warehouse worker ID used with whs_login.")],
        steps: Annotated[list[dict[str, Any]], Field(
            description=(
                "Objects with optional values, press, note and expect. expect supports page, step, "
                "title, message_kind, message substring, fields by name and label substrings by "
                "name."
            ),
            json_schema_extra={"minItems": 1, "maxItems": SCENARIO_STEPS_MAX},
        )],
        stop_on_mismatch: Annotated[bool, Field(description="Stop on the first failed expectation.")] = True,
        variables: Annotated[AnyMap, Field(
            description="Values for ${name} in steps, such as an order number saved by web_run_scenario.",
        )] = None,
    ) -> dict[str, Any]:
        ("Run steps on a logged-in worker. Poll whs_wait if running. Form mode lists uncheckable expectations "
         "separately.")
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
    def whs_log(
        worker: Annotated[str, Field(description="Test warehouse worker ID used with whs_login.")],
        lines: Annotated[int, Field(
            description="Number of tail lines.",
            json_schema_extra={"minimum": 1, "maximum": 500},
        )] = 60,
    ) -> dict[str, Any]:
        """Read the worker's step and page log, newest last, up to 64 KiB. Passwords are masked."""
        if not 1 <= lines <= 500:
            raise Refused("refused", "lines must be 1-500")
        text = steps_log.tail(worker, lines)
        if text is None:
            raise Refused("not_found", f"worker {worker.upper()} has no step log yet")
        return {"worker": worker.upper(), "log": text, "file": str(steps_log.path(worker))}

    @tool(RUNS, "writer")
    def whs_wait(
        worker: Annotated[str, Field(description="Test warehouse worker ID used with whs_login.")],
        seconds: Annotated[float | None, Field(
            description=(
                "Seconds to wait. Omit for the service's call budget. A lower service cap takes "
                "priority."
            ),
        )] = None,
    ) -> dict[str, Any]:
        """Collect a pending whs_run_scenario result. If still running, wait again before acting on the worker."""
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
