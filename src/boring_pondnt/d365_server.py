"""boring-pondnt-d365: typed tools on d365fo.tools for building, syncing and running a D365 dev box.

    boring-pondnt-d365 serve [--powershell <exe>] [--state-dir <dir>]
    boring-pondnt-d365 serve --config <config.json> [...]

Read tools run in one long-lived Windows PowerShell with d365fo.tools loaded
(loading takes ~20 s after start); build, sync and best practice run as jobs.
Every text that leaves the server passes `redact` first: d365fo.tools prints
SQL connection strings with passwords.
"""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager

import anyio
import json
import sys
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__, sqlserver, xref
from .xref_tools import register_tools as register_xref_tools
from boring_service_kit.access import permitted
from boring_service_kit.calllog import CallLog
from .d365 import CALL_SECONDS_DEFAULT, Jobs, Session
from boring_service_kit.http import ServiceConfig, add_config_argument
from boring_service_kit.pond import attach_heartbeat
from boring_service_kit.tools import as_mcp_tool, default_state_dir, refuse_unknown_arguments
from boring_service_kit.outcomes import Outcome

# Compatibility names remain available here; their owner is d365_tools.
from .d365_tools import (INSTRUCTIONS, redact, register_tools as register_d365_tools)


def create_server(powershell: str = "powershell.exe", state_dir: Path | None = None, log_calls: bool = False,
                  token_verifier: Any = None, auth: Any = None, on_discover: Any = None,
                  start_session: bool = True, sql_server: str = "localhost",
                  sql_connect: Any = None, xref_index: xref.Xref | None = None,
                  xref_server: str | None = None, xref_database: str = "DYNAMICSXREFDB",
                  xref_driver: str | None = None,
                  xref_stall_seconds: float = xref.STALL_SECONDS_DEFAULT,
                  auth_server_provider: Any = None, call_budget: float | None = None) -> MCPServer:
    """`call_budget`: behind a Pond (ServiceConfig.call_limit) no call outlasts it; None: no cap.

    A server that authenticates callers authorizes every call by the caller's role
    (boring-service-kit's `permitted`): reader for status, results and xref; writer
    for builds, sync, best practice, environment control, jobs and SQL.
    """
    enforce_access = token_verifier is not None or auth_server_provider is not None
    if call_budget:
        xref_stall_seconds = min(xref_stall_seconds, call_budget) if xref_stall_seconds else call_budget
    state = state_dir or default_state_dir("boring-pondnt")
    # (database, timeout seconds) -> a function that opens one connection; tests pass SQLite
    sql_connect = sql_connect or (lambda database, timeout: sqlserver.odbc_connector(
        sql_server, database, None, timeout, app="boring-pondnt-d365"))
    session = Session(powershell, min(CALL_SECONDS_DEFAULT, call_budget) if call_budget else CALL_SECONDS_DEFAULT)
    jobs = Jobs(powershell, state)
    def shutdown() -> None:
        jobs.close_admission()
        session.close_admission()
        try:
            jobs.shutdown()
        finally:
            session.close()

    @asynccontextmanager
    async def lifespan(server: Any):
        try:
            yield
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(shutdown)

    mcp = MCPServer("boring-pondnt-d365", instructions=INSTRUCTIONS, version=__version__,
                    token_verifier=token_verifier, auth=auth, auth_server_provider=auth_server_provider,
                    lifespan=lifespan)
    attach_heartbeat(mcp, on_discover)
    log = CallLog(state / "calls-d365.jsonl" if log_calls else None)

    def guard(fn: Any, role: str) -> Any:
        return permitted(fn, role) if enforce_access else fn

    def tool(annotations: Any, role: str) -> Any:
        def register(fn: Any) -> Any:
            def safe(*args: Any, **kwargs: Any) -> dict[str, Any]:
                try:
                    return redact(fn(*args, **kwargs))
                except Outcome as outcome:
                    outcome.message = redact(outcome.message)
                    outcome.details = redact(outcome.details)
                    raise
            safe.__name__, safe.__doc__, safe.__wrapped__ = fn.__name__, fn.__doc__, fn  # type: ignore[attr-defined]
            mcp.tool(annotations=annotations)(as_mcp_tool(guard(safe, role), log))
            return fn
        return register

    register_d365_tools(tool, session, jobs, state, sql_connect, call_budget)

    # Lazy connector: discovery and PowerShell never need the reference database.
    # MCP 2.2 dispatches sync functions on worker threads. Session owns its PS lock;
    # xref owns a connection per query and does not wait for that lock.
    index = xref_index
    if index is None:
        def connect_xref() -> Any:
            connector = xref.odbc_connector(
                xref_server or sql_server, xref_database, xref_driver, xref_stall_seconds)
            index.source = connector.description
            return connector()

        # Identify the configured DB even if loading the driver fails. On first
        # query, retain the exact resolved driver description from the old server.
        connect_xref.description = f"{xref_driver or 'automatic ODBC driver'}; {xref_server or sql_server}; {xref_database}"
        index = xref.Xref(connect_xref)
    register_xref_tools(mcp, index, log, lambda fn: guard(fn, "reader"), call_budget)
    refuse_unknown_arguments(mcp)
    mcp._d365_session = session  # type: ignore[attr-defined]
    mcp._d365_jobs = jobs  # type: ignore[attr-defined]
    mcp._d365_close = shutdown  # type: ignore[attr-defined]  # pre-lifespan failure and fixtures
    if start_session:
        session.start()
    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="boring-pondnt-d365", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="answer over HTTP, configured by --config (no stdio)")
    serve.add_argument("--powershell", default="powershell.exe", help="Windows PowerShell 5.1 (d365fo.tools needs it)")
    serve.add_argument("--sql-server", default="localhost", help="SQL Server instance for d365_sql (default %(default)s)")
    serve.add_argument("--xref-server", help="xref instance (default: --sql-server)")
    serve.add_argument("--xref-database", default="DYNAMICSXREFDB")
    serve.add_argument("--xref-driver", help="ODBC driver; default: newest SQL Server driver")
    serve.add_argument("--xref-stall-seconds", type=float, default=xref.STALL_SECONDS_DEFAULT,
                       help="xref query timeout in seconds; 0 = never")
    serve.add_argument("--state-dir", type=Path)
    serve.add_argument("--no-call-log", action="store_true")
    add_config_argument(serve)
    args = parser.parse_args(argv)
    xref_options = dict(xref_server=args.xref_server, xref_database=args.xref_database,
                        xref_driver=args.xref_driver, xref_stall_seconds=args.xref_stall_seconds)
    servers: list[MCPServer] = []
    budget: list[float | None] = [None]
    def make_server(auth: Any = None, on_discover: Any = None) -> MCPServer:
        server = create_server(args.powershell, args.state_dir, not args.no_call_log,
                               on_discover=on_discover, sql_server=args.sql_server, call_budget=budget[0],
                               **xref_options, **(auth or {}))
        servers.append(server)
        return server

    try:
        if args.config:
            config = ServiceConfig(args.config)
            budget[0] = config.call_limit  # the Pond's bound with a pond section; none on loopback
            config.serve(make_server)
            return
        parser.error("serve requires --config: the D365 server speaks HTTP only; setup.ps1 writes it")
    except Outcome as outcome:
        print(json.dumps(redact(outcome.as_dict())), file=sys.stderr)
        raise SystemExit(2) from None
    finally:
        for server in servers:
            server._d365_close()  # also covers transport failure before lifespan entry


if __name__ == "__main__":
    main()
