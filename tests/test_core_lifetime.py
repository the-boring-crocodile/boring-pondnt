"""Core server lifetime with synthetic managers; no external processes."""
import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

from boring_pondnt import server


def call(mcp):
    return asyncio.run(mcp.call_tool("job_list", {}))


def test_concurrent_first_calls_share_one_manager(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    instances = []

    class Manager:
        def __init__(self, root):
            instances.append(self)
            entered.set()
            assert release.wait(5)
        def list(self, limit):
            return []
        def shutdown(self):
            self.closed = True

    monkeypatch.setattr(server.jobs, "Jobs", Manager)
    mcp = server.create_server(state_dir=tmp_path / "state")

    async def scenario():
        app = mcp.streamable_http_app()
        async with app.router.lifespan_context(app):
            with ThreadPoolExecutor(2) as pool:
                first = pool.submit(call, mcp)
                assert entered.wait(3)
                second_entered = threading.Event()
                def second_call():
                    second_entered.set()
                    return call(mcp)
                second = pool.submit(second_call)
                try:
                    assert second_entered.wait(3)
                    # The first constructor is blocked while another call arrives.
                    assert len(instances) == 1
                finally:
                    release.set()
                assert not first.result(5).is_error
                assert not second.result(5).is_error
        assert len(instances) == 1
        assert instances[0].closed

    asyncio.run(scenario())


def test_unused_server_shutdown_does_not_create_manager(tmp_path, monkeypatch):
    def forbidden(*args):
        raise AssertionError("discovery or shutdown created a manager")
    monkeypatch.setattr(server.jobs, "Jobs", forbidden)
    state = tmp_path / "absent"
    mcp = server.create_server(state_dir=state)

    async def scenario():
        app = mcp.streamable_http_app()
        async with app.router.lifespan_context(app):
            await mcp.list_tools()
        result = await mcp.call_tool("job_list", {})
        assert result.is_error
        assert json.loads(result.content[0].text)["code"] == "unavailable"

    asyncio.run(scenario())
    assert not state.exists()
