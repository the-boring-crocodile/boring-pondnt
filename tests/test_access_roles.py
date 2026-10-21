"""Reader and writer grants through the real authenticated HTTP path.

The grant's role is the configuration's `access` entry; the token is checked by
introspection against the fake control plane, exactly as in production. A
refused call must have no effect: no file, no process, no stub call.
"""

from __future__ import annotations

import asyncio
import base64
import sys

import pytest

from boring_pondnt import d365_server
from boring_service_kit.outcomes import Failed
from test_pond_service import Service
from test_xref import db, index  # noqa: F401  (pytest fixtures)

posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell command")


class RoleService(Service):
    """The core with the Pond's grant set to `role`."""

    def __init__(self, tmp_path, role: str) -> None:
        self.role = role
        super().__init__(tmp_path)

    def make_server(self, auth):
        policy = auth["token_verifier"].introspection.policy
        caller = policy.grants["pond"]
        policy.grants["pond"] = type(caller)(caller.issuer, caller.subject, caller.author, self.role)
        return super().make_server(auth)


class RoleD365(RoleService):
    """The D365 server with recording stubs instead of PowerShell, SQL and jobs."""

    def __init__(self, tmp_path, role: str, xref_index) -> None:
        self.xref_index = xref_index
        self.record: list[str] = []
        super().__init__(tmp_path, role)

    def make_server(self, auth):
        RoleService.make_server(self, auth)  # applies the role; the core server it builds is unused
        server = d365_server.create_server(state_dir=self.tmp_path / "d365", start_session=False,
                                           xref_index=self.xref_index,
                                           sql_connect=lambda database, timeout: self.connect_sql, **auth)
        session, jobs = server._d365_session, server._d365_jobs
        session.call = lambda script, **kwargs: self.record.append(script) or []
        session.ensure_ready = lambda: None
        jobs.start = lambda kind, script, **kwargs: self.record.append(f"job:{kind}") or {"job_id": "x"}
        jobs.stop = lambda job_id: self.record.append(f"stop:{job_id}") or {}
        self.server = server
        return server

    def connect_sql(self):
        self.record.append("sql")
        raise Failed("unavailable", "no SQL Server in this test")

    async def __aexit__(self, *exc: object) -> None:
        await super().__aexit__(*exc)
        self.server._d365_close()


def run(coro):
    return asyncio.run(coro)


def forbidden(outcome) -> bool:
    is_error, data = outcome
    return is_error and data["code"] == "forbidden"


def test_a_reader_cannot_write_run_or_control_jobs(tmp_path):
    existing = tmp_path / "existing.txt"
    existing.write_text("original")
    created = tmp_path / "created.txt"

    async def scenario() -> None:
        async with RoleService(tmp_path, "reader") as service:
            _, read = await service.result("file_read", {"path": str(existing)})
            sha = read["sha256"]
            assert forbidden(await service.result(
                "file_write", {"path": str(created), "content": "x", "expected_sha256": None}))
            assert forbidden(await service.result(
                "file_write", {"path": str(existing), "content": "x", "expected_sha256": sha}))
            assert forbidden(await service.result("file_patch", {
                "path": str(existing), "expected_sha256": sha,
                "edits": [{"old_text": "original", "new_text": "patched"}]}))
            assert forbidden(await service.result("file_write_bytes", {
                "path": str(created), "data": base64.b64encode(b"x").decode(), "expected_sha256": None}))
            assert forbidden(await service.result("exec", {"command": f"echo x > {created}"}))
            assert forbidden(await service.result("job_start", {"command": f"echo x > {created}"}))
            assert forbidden(await service.result("job_stop", {"job_id": "0" * 32}))
            _, listed = await service.result("job_list", {})
            assert listed["jobs"] == []
        assert existing.read_text() == "original" and not created.exists()
        assert not (tmp_path / "state" / "jobs").exists() or not any((tmp_path / "state" / "jobs").iterdir())
    run(scenario())


def test_a_readers_batch_with_a_writer_step_runs_no_step(tmp_path):
    """Batch preflight checks every step's role before any step runs: refused whole."""
    created = tmp_path / "batch.txt"
    (tmp_path / "seen.txt").write_text("seen")

    async def scenario() -> None:
        async with RoleService(tmp_path, "reader") as service:
            is_error, refusal = await service.result("batch", {"operations": [
                {"id": "read", "method": "file_read", "arguments": {"path": str(tmp_path / "seen.txt")}},
                {"id": "write", "method": "file_write",
                 "arguments": {"path": str(created), "content": "batch", "expected_sha256": None}}]})
            assert is_error and refusal["code"] == "forbidden" and refusal["details"]["step"] == "write"
            for method, arguments in (("exec", {"command": f"echo x > {created}"}),
                                      ("job_start", {"command": f"echo x > {created}"}),
                                      ("job_stop", {"job_id": "0" * 32})):
                assert forbidden(await service.result(
                    "batch", {"operations": [{"id": "one", "method": method, "arguments": arguments}]}))
            # Reads stay available, in a batch too.
            is_error, result = await service.result("batch", {"operations": [
                {"id": "read", "method": "file_read", "arguments": {"path": str(tmp_path / "seen.txt")}},
                {"id": "list", "method": "file_list", "arguments": {"path": str(tmp_path)}},
                {"id": "jobs", "method": "job_list", "arguments": {}}]})
            assert not is_error and [r["status"] for r in result["results"]] == ["succeeded"] * 3
        assert not created.exists()
    run(scenario())


def test_a_reader_reads(tmp_path):
    (tmp_path / "a.txt").write_text("needle")

    async def scenario() -> None:
        async with RoleService(tmp_path, "reader") as service:
            for tool, arguments in (("bootstrap", {}),
                                    ("file_read", {"path": str(tmp_path / "a.txt")}),
                                    ("file_read_bytes", {"path": str(tmp_path / "a.txt")}),
                                    ("file_list", {"path": str(tmp_path)}),
                                    ("file_search", {"path": str(tmp_path), "query": "needle"}),
                                    ("job_list", {})):
                is_error, data = await service.result(tool, arguments)
                assert not is_error, (tool, data)
            # No job exists for a reader to observe: these reach the job manager and answer not_found.
            for tool, arguments in (("job_wait", {"job_ids": ["0" * 32], "wait_seconds": 0}),
                                    ("job_output", {"job_id": "0" * 32})):
                is_error, data = await service.result(tool, arguments)
                assert is_error and data["code"] == "not_found", (tool, data)
    run(scenario())


@posix
def test_a_writer_writes_runs_and_controls_jobs(tmp_path):
    created, ran, batched = tmp_path / "created.txt", tmp_path / "ran.txt", tmp_path / "batch.txt"

    async def scenario() -> None:
        async with RoleService(tmp_path, "writer") as service:
            is_error, _ = await service.result(
                "file_write", {"path": str(created), "content": "x", "expected_sha256": None})
            assert not is_error
            is_error, result = await service.result("exec", {"command": f"echo x > {ran}"})
            assert not is_error and result["exit_code"] == 0, result
            is_error, started = await service.result("job_start", {"command": "sleep 30"})
            assert not is_error, started
            is_error, stopped = await service.result("job_stop", {"job_id": started["job_id"]})
            assert not is_error, stopped
            is_error, result = await service.result("batch", {"operations": [
                {"id": "w", "method": "file_write",
                 "arguments": {"path": str(batched), "content": "b", "expected_sha256": None}}]})
            assert not is_error and result["results"][0]["status"] == "succeeded", result
        assert created.exists() and ran.exists() and batched.exists()
    run(scenario())


def test_d365_reader_is_refused_admin_build_sql_and_jobs(tmp_path, index):  # noqa: F811
    async def scenario() -> None:
        async with RoleD365(tmp_path, "reader", index) as service:
            for tool, arguments in (("d365_environment", {"action": "stop", "services": "aos"}),
                                    ("d365_sql", {"sql": "DELETE FROM SalesTable"}),
                                    ("d365_build", {"module": "ContosoExt"}),
                                    ("d365_db_sync", {}),
                                    ("d365_best_practice", {"module": "ContosoExt", "model": "ContosoExt"}),
                                    ("d365_job", {"job_id": "x", "stop": True})):
                assert forbidden(await service.result(tool, arguments)), tool
            assert service.record == []  # no PowerShell script, SQL connection or job was started
            for tool, arguments in (("d365_status", {}), ("xref_info", {}),
                                    ("xref_names", {"name": "ContosoBase"})):
                is_error, data = await service.result(tool, arguments)
                assert not is_error, (tool, data)
    run(scenario())


# Every tool, by the role it needs. The reader tests above refuse each writer tool;
# a tool added to a server fails the inventory test until it is classified here.
CORE_WRITER = {"file_write", "file_patch", "file_write_bytes", "exec", "job_start", "job_stop"}
CORE_READER = {"bootstrap", "file_read", "file_read_bytes", "file_list", "file_search",
               "job_list", "job_wait", "job_output", "batch"}
D365_WRITER = {"d365_environment", "d365_sql", "d365_build", "d365_db_sync", "d365_best_practice", "d365_job"}
D365_READER = {"d365_status", "d365_models", "d365_results",
               "xref_info", "xref_names", "xref_references", "xref_hierarchy"}


def test_every_tool_is_classified(tmp_path, index):  # noqa: F811
    from boring_pondnt import server as core_server
    from boring_service_kit.access import permitted

    core = core_server.create_server(state_dir=tmp_path / "core")
    d365 = d365_server.create_server(state_dir=tmp_path / "d365", start_session=False, xref_index=index)
    try:
        assert {t.name for t in asyncio.run(core.list_tools())} == CORE_WRITER | CORE_READER
        assert {t.name for t in asyncio.run(d365.list_tools())} == D365_WRITER | D365_READER
    finally:
        d365._d365_close()
    # Registration names a role; anything but reader/writer stops the server from starting.
    for role in ("", "admin", None):
        with pytest.raises(ValueError):
            permitted(lambda: None, role)


def test_d365_writer_dispatches(tmp_path, index):  # noqa: F811
    async def scenario() -> None:
        async with RoleD365(tmp_path, "writer", index) as service:
            # d365_environment is refused under a Pond call budget; this server has none.
            is_error, data = await service.result("d365_environment", {"action": "stop", "services": "aos"})
            assert not is_error, data
            is_error, data = await service.result("d365_build", {"module": "ContosoExt"})
            assert not is_error, data
            is_error, data = await service.result("d365_sql", {"sql": "SELECT 1"})
            assert is_error and data["code"] != "forbidden"  # reached the (failing) SQL stub
        assert any("Stop-D365Environment" in s for s in service.record)
        assert "job:build" in service.record and "sql" in service.record
    run(scenario())

