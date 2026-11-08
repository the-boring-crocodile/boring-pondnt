"""OData service for a D365 environment (authenticated HTTP only)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field, StrictInt
from boring_service_kit.access import permitted
from boring_service_kit.calllog import CallLog
from boring_service_kit.http import ServiceConfig, add_config_argument
from boring_service_kit.outcomes import Outcome, Refused
from boring_service_kit.pond import attach_heartbeat
from boring_service_kit.tools import READ_ONLY, WRITES, as_mcp_tool, default_state_dir, refuse_unknown_arguments

from . import __version__
from .d365data_client import (
    DMF_TEXT_LIMITS, MAX_NEXT_CHARS, MAX_TEXT, NAME, RESULT_CHARS_DEFAULT, RESULT_CHARS_MAX,
    WRITE_OPERATIONS, Config, DataClient, _check_max_chars, mask_error,
)
from .d365data_submissions import SUBMISSION_ID, Submissions
from .tool_schema import AnyMap

ResultChars = Annotated[StrictInt, Field(
    description="Maximum compact JSON characters in the returned data, excluding status and paging fields.",
    json_schema_extra={"minimum": 1, "maximum": RESULT_CHARS_MAX},
)]


# Each server its own name: support bundles collect several state folders flat.
CALL_LOG = "calls-d365data.jsonl"


def create_server(client: DataClient, state_dir: Path | None = None, log_calls: bool = False,
                  token_verifier: Any = None, auth: Any = None, on_discover: Any = None,
                  auth_server_provider: Any = None, call_budget: float | None = None) -> MCPServer:
    if call_budget:
        client.config.timeout = min(client.config.timeout, max(1.0, call_budget - 5.0))
    enforce_access = token_verifier is not None or auth_server_provider is not None
    state = state_dir or default_state_dir("boring-pondnt")
    import_wait = 100.0 if call_budget is None else max(0.0, call_budget - 5.0)
    submissions = Submissions(client, state / "dmf-submissions.json")
    mcp = MCPServer("boring-pondnt-d365data", instructions="Read and change D365 data through public OData entities, entity-set actions and DMF package tools.",
                    version=__version__, token_verifier=token_verifier, auth=auth,
                    auth_server_provider=auth_server_provider)
    attach_heartbeat(mcp, on_discover)
    log = CallLog(state / CALL_LOG if log_calls else None)

    def tool(annotations: Any, role: str) -> Any:
        def register(fn: Any) -> Any:
            def safe(*args: Any, **kwargs: Any) -> dict[str, Any]:
                try:
                    return fn(*args, **kwargs)
                except Outcome as outcome:
                    outcome.message = mask_error(outcome.message)
                    outcome.details = mask_error(outcome.details)
                    raise
            safe.__name__, safe.__doc__, safe.__wrapped__ = fn.__name__, fn.__doc__, fn
            guarded = permitted(safe, role) if enforce_access else safe
            mcp.tool(annotations=annotations)(as_mcp_tool(guarded, log))
            return fn
        return register

    @tool(READ_ONLY, "reader")
    def odata_entities(
        search: Annotated[str | None, Field(
            description="Case-insensitive substring of an entity-set name. Use entity for an exact set.",
        )] = None,
        entity: Annotated[str | None, Field(
            description="Public entity-set name from odata_entities.",
            json_schema_extra={"pattern": NAME.pattern, "maxLength": 200},
        )] = None,
        refresh: Annotated[bool, Field(description="Reload cached metadata before answering.")] = False,
        max_chars: ResultChars = RESULT_CHARS_DEFAULT,
    ) -> dict[str, Any]:
        """Read public entity sets, or one set's fields, types and keys. After a build and sync, use refresh=true."""
        return client.entities(search, entity, refresh, max_chars=max_chars)

    @tool(READ_ONLY, "reader")
    def odata_query(
        entity: Annotated[str | None, Field(
            description="Public entity-set name from odata_entities. Required for a new query.",
            json_schema_extra={"pattern": NAME.pattern, "maxLength": 200},
        )] = None,
        filter: Annotated[str | None, Field(
            description=(
                "OData $filter expression without $filter=. Use single quotes for strings (double "
                "embedded apostrophes), qualified enum literals like Namespace.Enum'Member', "
                "YYYY-MM-DD dates or ISO timestamps with an offset."
            ),
            json_schema_extra={"maxLength": MAX_TEXT},
        )] = None,
        select: Annotated[str | None, Field(
            description="Comma-separated OData field names.",
            json_schema_extra={"maxLength": MAX_TEXT},
        )] = None,
        order_by: Annotated[str | None, Field(
            description="Comma-separated OData fields with optional asc or desc.",
            json_schema_extra={"maxLength": MAX_TEXT},
        )] = None,
        top: Annotated[StrictInt | None, Field(
            description=(
                "Page size. Defaults to 20 for a new query. On continuation, match the link's page "
                "size if present. Otherwise it defaults to 500."
            ),
            json_schema_extra={"minimum": 1, "maximum": 500},
        )] = None,
        company: Annotated[str | None, Field(
            description=(
                "Legal entity for company-specific sets only. Omit to use the configured or mapped "
                "user's default, or set cross_company for all companies."
            ),
            json_schema_extra={"maxLength": MAX_TEXT},
        )] = None,
        cross_company: Annotated[bool, Field(
            description="With no company, read all companies and skip the configured default.",
        )] = False,
        next: Annotated[str | None, Field(
            description=(
                "Use the returned URL unchanged. Only top, max_chars and include_etag may "
                "accompany it. The link carries count; count=true is refused."
            ),
            json_schema_extra={"maxLength": MAX_NEXT_CHARS},
        )] = None,
        max_chars: ResultChars = RESULT_CHARS_DEFAULT,
        include_etag: Annotated[bool, Field(description="Include @odata.etag on rows.")] = False,
        count: Annotated[bool | None, Field(description="Ask D365 for the total matching rows before page cuts.")] = None,
    ) -> dict[str, Any]:
        ("Read a page or follow its next link with odata_query. A link skips the whole page. If rows were "
         "cut, narrow select, reduce top or raise max_chars before paging.")
        return client.query(entity, filter, select, order_by, top, company, cross_company, next,
                            max_chars=max_chars, include_etag=include_etag, count=count)

    @tool(WRITES, "writer")
    def odata_write(
        entity: Annotated[str, Field(
            description="Public entity-set name from odata_entities.",
            json_schema_extra={"pattern": NAME.pattern, "maxLength": 200},
        )],
        op: Annotated[str, Field(
            description="Create takes values. Update takes key and values. Delete takes key.",
            json_schema_extra={"enum": WRITE_OPERATIONS},
        )],
        key: Annotated[AnyMap, Field(
            description=(
                "Object of metadata key fields. A selected company can supply dataAreaId. Use "
                "member names for enum keys, YYYY-MM-DD dates and offset ISO timestamps. String "
                "keys for update/delete cannot contain / or \\."
            ),
        )] = None,
        values: Annotated[AnyMap, Field(
            description="Object of field names and values for create or update. Flat values only.",
        )] = None,
        company: Annotated[str | None, Field(
            description="Legal entity for a company-specific set. Omit to use the configured company if set; "
                        "without one, a create lands in the service user's default company. Update/delete: only "
                        "for sets whose key has dataAreaId.",
            json_schema_extra={"maxLength": MAX_TEXT},
        )] = None,
        max_chars: ResultChars = RESULT_CHARS_DEFAULT,
        include_etag: Annotated[bool, Field(description="Include @odata.etag on the returned record.")] = False,
        etag: Annotated[str | None, Field(
            description="Update/delete only if the record is unchanged: its @odata.etag from odata_query "
                        "include_etag. A changed record answers conflict and nothing is written.",
            json_schema_extra={"maxLength": 512},
        )] = None,
    ) -> dict[str, Any]:
        ("Write a D365 record. An update can return record:null when D365 sends no body. Use odata_query to "
         "verify values or check a lost response before retrying.")
        return client.write(entity, op, key, values, company, max_chars=max_chars, include_etag=include_etag,
                            etag=etag)

    @tool(WRITES, "writer")
    def odata_action(
        entity: Annotated[str, Field(
            description="Public entity-set name from odata_entities.",
            json_schema_extra={"pattern": NAME.pattern, "maxLength": 200},
        )],
        action: Annotated[str, Field(
            description="Collection action name on the entity set.",
            json_schema_extra={"pattern": NAME.pattern, "maxLength": 200},
        )],
        arguments: Annotated[AnyMap, Field(description="Object of action parameter names and values.")] = None,
        max_chars: ResultChars = RESULT_CHARS_DEFAULT,
    ) -> dict[str, Any]:
        ("Run a collection action in D365. Use the DMF tools for package actions. Check the action's result "
         "before retrying a lost response.")
        return client.action(entity, action, arguments, max_chars=max_chars)

    @tool(WRITES, "writer")
    def dmf_export(
        definition_group: Annotated[str, Field(
            description="Name of the existing data project in D365.",
            json_schema_extra={"minLength": 1, "maxLength": DMF_TEXT_LIMITS["definition_group"]},
        )],
        company: Annotated[str, Field(
            description="D365 legal entity for this project run.",
            json_schema_extra={"minLength": 1, "maxLength": DMF_TEXT_LIMITS["company"]},
        )],
        package_name: Annotated[str | None, Field(
            description="Optional name for the generated package.",
            json_schema_extra={"minLength": 1, "maxLength": DMF_TEXT_LIMITS["package_name"]},
        )] = None,
    ) -> dict[str, Any]:
        ("Start exporting an existing project. Poll dmf_status with execution_id, then use download=true to "
         "save a successful export ZIP.")
        return client.dmf_export(definition_group, company, package_name)

    @tool(WRITES, "writer")
    def dmf_import(
        definition_group: Annotated[str, Field(
            description="Name of the existing data project in D365.",
            json_schema_extra={"minLength": 1, "maxLength": DMF_TEXT_LIMITS["definition_group"]},
        )],
        package_path: Annotated[str, Field(
            description=(
                "ZIP path inside the configured package folder, such as the package_path from "
                "dmf_status(download=true). Maximum 64 MiB."
            ),
        )],
        company: Annotated[str, Field(
            description="D365 legal entity for this project run.",
            json_schema_extra={"minLength": 1, "maxLength": DMF_TEXT_LIMITS["company"]},
        )],
        execute: Annotated[bool, Field(
            description="False attaches the package to the project without starting an execution or batch job.",
        )] = True,
        overwrite: Annotated[bool, Field(description="Set false for composite entities.")] = True,
    ) -> dict[str, Any]:
        ("Submit one local ZIP import. If submitting or busy, poll dmf_status with submission_id. When you "
         "have execution_id, use it to check execution. Check D365 history before retrying a lost response.")
        return submissions.start(definition_group, package_path, company, execute, overwrite, import_wait)

    def status(execution_id: str | None, submission_id: str | None,
               download: bool, max_chars: int) -> dict[str, Any]:
        if (execution_id is None) == (submission_id is None):
            raise Refused("invalid_request", "supply exactly one of execution_id and submission_id")
        if submission_id is None:
            return client.dmf_status(execution_id, download, max_chars=max_chars)
        _check_max_chars(max_chars)
        receipt, execute = submissions.status(submission_id)
        if receipt["state"] == "submitted" and execute:
            try:
                receipt.update(client.dmf_status(receipt["execution_id"], download, max_chars=max_chars))
            except Outcome as error:
                receipt["status_error"] = mask_error(error.as_dict(), omit_all_urls=True)
        elif receipt["state"] == "submitted" and download:
            receipt["download_note"] = "No download attempted: the import ran with execute=false, so no execution ran"
        return receipt

    writer_status = permitted(status, "writer") if enforce_access else status

    @tool(WRITES, "reader")
    def dmf_status(
        execution_id: Annotated[str | None, Field(
            description="Opaque D365 execution ID from an import or export. Pass it unchanged.",
            json_schema_extra={"minLength": 1, "maxLength": DMF_TEXT_LIMITS["execution_id"]},
        )] = None,
        download: Annotated[bool, Field(
            description="Save a successful or partially successful export as a new ZIP and return package_path.",
        )] = False,
        max_chars: ResultChars = RESULT_CHARS_DEFAULT,
        submission_id: Annotated[str | None, Field(
            description=(
                "Receipt from a submitting or busy import. Once submitted, use execution_id for "
                "execution status."
            ),
            json_schema_extra={"pattern": f"^{SUBMISSION_ID.pattern}$"},
        )] = None,
    ) -> dict[str, Any]:
        ("Read status and errors. Give exactly one ID. Poll again while running. A successful export can be "
         "saved with download=true, which requires a writer.")
        fn = writer_status if download else status
        return fn(execution_id, submission_id, download, max_chars)

    refuse_unknown_arguments(mcp)
    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="boring-pondnt-d365data", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="answer over authenticated HTTP")
    serve.add_argument("--d365data", type=Path, required=True)
    serve.add_argument("--state-dir", type=Path)
    serve.add_argument("--no-call-log", action="store_true")
    add_config_argument(serve)
    args = parser.parse_args(argv)
    try:
        if not args.config:
            parser.error("serve requires --config: the d365data server speaks HTTP only")
        client = DataClient(Config.load(args.d365data))
        config = ServiceConfig(args.config)
        def make_server(auth: Any = None, on_discover: Any = None) -> MCPServer:
            return create_server(client, args.state_dir, not args.no_call_log, on_discover=on_discover,
                                 call_budget=config.call_limit, **(auth or {}))
        config.serve(make_server)
    except Outcome as outcome:
        print(json.dumps(mask_error(outcome.as_dict())), file=sys.stderr)
        raise SystemExit(2) from None
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps(mask_error({"status": "failed", "code": "config", "message": str(error)})), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
