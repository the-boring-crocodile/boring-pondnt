"""boring-pondnt-core as a Pond service: the contract, plus only allowed Ponds.

The contract itself is tested in boring-service-kit; these check pondNT's servers with it.

Real SDK app over httpx2's ASGI transport; a fake control plane (a stdlib HTTP
server in a thread) answers /introspect and /services/register. Modelled on
boring-canvas's tests/test_mcp_service.py.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx2
import pytest

from boring_service_kit.http import ServiceConfig
from boring_pondnt.server import create_server

OWN_URL = "http://127.0.0.1:18520/mcp"
CLIENT_ID, SECRET = "devbox-client", "devbox-secret"
VERSION = "2026-07-28"


class FakeControlPlane:
    def __init__(self) -> None:
        self.tokens: dict[str, dict[str, Any]] = {}
        self.introspect_status = 200
        self.register: tuple[int, bytes] = (200, b"{}")
        self.requests: list[dict[str, Any]] = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                fake.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                if self.path == "/introspect":
                    token = parse_qs(body.decode())["token"][0]
                    status, payload = fake.introspect_status, json.dumps(fake.tokens.get(token, {"active": False}))
                elif self.path == "/services/register":
                    status, payload = fake.register[0], fake.register[1].decode()
                else:
                    status, payload = 404, "{}"
                data = payload.encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class RecordingWatchdog:
    def __init__(self) -> None:
        self.pings: list[str] = []

    def pinged(self, source: str = "") -> None:
        self.pings.append(source)


def _meta(client: str = "tester") -> dict[str, Any]:
    return {"io.modelcontextprotocol/protocolVersion": VERSION,
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": client, "version": "1"}}


def _headers(method: str, name: str | None = None, token: str | None = None) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "MCP-Protocol-Version": VERSION, "Mcp-Method": method}
    if name is not None:
        headers["Mcp-Name"] = name
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class Service:
    """The app with its lifespan running, and an HTTP client into it."""

    def __init__(self, tmp_path: Path) -> None:
        self.cp = FakeControlPlane()
        now = int(time.time())
        good = {"active": True, "aud": OWN_URL, "exp": now + 300, "sub": "pond", "client_id": "control-plane"}
        self.cp.tokens.update({
            "pond": good, "garden": dict(good, sub="garden"),
            "other-service": dict(good, aud="http://127.0.0.1:18512/mcp"),
            "expired": dict(good, exp=now - 1), "no-sub": {k: v for k, v in good.items() if k != "sub"}})
        self.watchdog = RecordingWatchdog()
        self.tmp_path = tmp_path
        # What tools/devbox-start.ps1 writes: the Crocodile through the tunnel, the Pond "pond" the only caller.
        (tmp_path / "secret").write_text(SECRET)
        (tmp_path / "config.json").write_text(json.dumps({
            "url": OWN_URL, "bind": "127.0.0.1", "port": 18520,
            "oauth": {"issuer": self.cp.url, "introspection_url": self.cp.url + "/introspect",
                      "client_id": CLIENT_ID, "secret_file": "secret"},
            "pond": {"url": self.cp.url, "client_id": CLIENT_ID, "secret_file": "secret"},
            "access": [{"subject": "pond", "role": "writer", "author": "pond"}]}))
        config = ServiceConfig(tmp_path / "config.json")
        self.call_limit = config.call_limit  # registered with a Pond: the Pond's bound
        self.app = config.app(self.make_server(config.auth))

    def make_server(self, auth):
        return create_server(state_dir=self.tmp_path / "state", on_discover=self.watchdog.pinged,
                             call_budget=self.call_limit, **auth)

    async def __aenter__(self) -> "Service":
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.task = asyncio.create_task(self.app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}},
                                                 self.inbox.get, self.outbox.put))
        await self.inbox.put({"type": "lifespan.startup"})
        assert (await self.outbox.get())["type"] == "lifespan.startup.complete"
        self.client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app, raise_app_exceptions=False),
                                         base_url="http://127.0.0.1:18520")
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.client.aclose()
        await self.inbox.put({"type": "lifespan.shutdown"})
        assert (await self.outbox.get())["type"] == "lifespan.shutdown.complete"
        await self.task
        self.cp.close()

    async def call(self, tool: str, arguments: dict[str, Any], token: str | None = "pond") -> httpx2.Response:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": tool, "arguments": arguments, "_meta": _meta()}}
        return await self.client.post("/mcp", json=body, headers=_headers("tools/call", tool, token))

    async def result(self, tool: str, arguments: dict[str, Any], token: str = "pond") -> tuple[bool, dict[str, Any]]:
        response = await self.call(tool, arguments, token)
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        return bool(result.get("isError")), json.loads(result["content"][0]["text"])


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_an_allowed_pond_uses_the_tools(tmp_path):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            (tmp_path / "hello.txt").write_text("hi")
            is_error, listed = await service.result("file_list", {"path": str(tmp_path)}, token="pond")
            assert not is_error and "hello.txt" in [e["name"] for e in listed["entries"]]
    run(scenario())


def test_a_valid_token_for_another_pond_is_refused(tmp_path):
    """Defence in depth for an admin shell: the grant may say more, the server says only these Ponds."""
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            response = await service.call("file_list", {"path": str(tmp_path)}, token="garden")
            assert response.status_code == 401
    run(scenario())


@pytest.mark.parametrize("token", [None, "unknown", "other-service", "expired", "no-sub"])
def test_tokens_that_are_not_ours_are_refused(tmp_path, token):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            response = await service.call("job_list", {}, token=token)
            assert response.status_code == 401, (token, response.status_code)
    run(scenario())


def test_discover_needs_no_token_and_feeds_the_watchdog(tmp_path):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            body = {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": _meta("orchard")}}
            response = await service.client.post("/mcp", json=body, headers=_headers("server/discover"))
            assert response.status_code == 200, response.text
            assert service.watchdog.pings == ["orchard"]
            assert not [r for r in service.cp.requests if r["path"] == "/introspect"]  # no token was checked
    run(scenario())


def test_a_mismatched_method_header_does_not_bypass_auth(tmp_path):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "job_list", "arguments": {}, "_meta": _meta()}}
            response = await service.client.post("/mcp", json=body, headers=_headers("server/discover"))
            assert response.status_code != 200 or "result" not in response.json()
    run(scenario())


def test_refusals_read_as_the_worker_reads_them(tmp_path):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            is_error, refusal = await service.result("file_read", {"path": "relative"})
            # The worker's rule (services.py, _tool_refusal): JSON with a code and a message.
            assert is_error and refusal["code"] == "invalid_request" and isinstance(refusal["message"], str)
    run(scenario())


def test_an_unreachable_control_plane_is_a_failure_not_a_bad_token(tmp_path):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            service.cp.introspect_status = 503
            response = await service.call("job_list", {})
            assert response.status_code == 500
    run(scenario())


def test_the_call_budget_caps_and_says_so(tmp_path):
    async def scenario() -> None:
        async with Service(tmp_path) as service:
            is_error, refusal = await service.result("exec", {"command": "echo hi", "timeout_seconds": 30})
            assert is_error and refusal["code"] == "invalid_request" and "job_start" in refusal["message"]
            assert service.call_limit == 28.0  # the Pond's 30 s less the answer's margin
            is_error, refusal = await service.result("job_wait", {"job_ids": [], "wait_seconds": 40})
            assert is_error and "28" in refusal["message"]
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": _meta()}}
            listed = (await service.client.post("/mcp", json=body, headers=_headers("tools/list", token="pond"))).json()
            tools = {t["name"]: t for t in listed["result"]["tools"]}
            assert "28 s here" in tools["exec"]["description"]
            assert all(len(t["description"]) <= 120 for t in tools.values()), \
                {n: len(t["description"]) for n, t in tools.items()}
            assert tools["exec"]["inputSchema"]["properties"]["timeout_seconds"]["default"] == 28
            assert "crlf|lf" in tools["file_write"]["description"]
    run(scenario())


class SourceCacheService(Service):
    """The same contract around Sourcecache (boring-sourcecache-d365), on the synthetic corpus."""

    def make_server(self, auth):
        import corpus
        from boring_sourcecache_d365.server import create_server as create_sourcecache
        from boring_sourcecache_d365.sourcecache import build

        base = corpus.write(self.tmp_path / "src")
        db = self.tmp_path / "idx.db"
        build(str(base), str(db), "synthetic")
        return create_sourcecache([str(db)], self.tmp_path / "state", on_discover=self.watchdog.pinged, **auth)


def test_sourcecache_as_a_pond_service(tmp_path):
    async def scenario() -> None:
        async with SourceCacheService(tmp_path) as service:
            is_error, found = await service.result("label_lookup", {"label_id": "@SYS100"}, token="pond")
            assert not is_error and found["index"]["name"] == "synthetic" and found["labels"]
            assert (await service.call("source_info", {}, token="garden")).status_code == 401
            body = {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": _meta("pond")}}
            assert (await service.client.post("/mcp", json=body, headers=_headers("server/discover"))).status_code == 200
            assert service.watchdog.pings == ["pond"]
    run(scenario())


def test_services_do_not_import_the_core():
    """The services and the shared Pond-service code never import the core's modules."""
    import re

    core = {"server", "execs", "jobs", "process", "textfiles", "binfiles", "listing", "disk", "selfcheck"}
    import boring_pondnt

    src = Path(boring_pondnt.__file__).parent  # the installed package: the release has no src/
    for name in ("d365.py", "d365_server.py", "d365_tools.py", "xref.py", "xref_tools.py"):
        text = (src / name).read_text()
        modules = set(re.findall(r"^\s*from \.(\w+) import", text, re.M))
        for group in re.findall(r"^\s*from \. import ([\w, ]+)", text, re.M):
            modules |= {m.strip() for m in group.split(",")}
        assert not modules & core, (name, modules & core)
