"""Combined server boundaries using SQLite, fake PS, and the installed MCP dispatch/transports."""
import asyncio
import json
import os
import sys
import threading
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from boring_pondnt import xref
from boring_pondnt.d365_server import create_server
from test_xref import db, index  # noqa: F401  (pytest fixture)
from test_d365 import fake_ps, ready_server  # noqa: F401  (pytest fixture)
from test_pond_service import Service, _meta, _headers


def data(result):
    return json.loads(result.content[0].text)


def test_xref_works_without_powershell(index, tmp_path):  # noqa: F811
    server = create_server(powershell=str(tmp_path / 'absent'), state_dir=tmp_path, xref_index=index)
    async def scenario():
        result = await server.call_tool('xref_names', {'name': 'ContosoBase'})
        assert not result.is_error and data(result)['names']
    try:
        asyncio.run(scenario())
    finally:
        server._d365_close()


def test_blocked_powershell_does_not_block_xref(index, tmp_path, monkeypatch):  # noqa: F811
    server = create_server(state_dir=tmp_path, start_session=False, xref_index=index)
    entered, release = threading.Event(), threading.Event()
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5), 'test must release the blocked PS call'
        return []
    monkeypatch.setattr(server._d365_session, 'call', blocked)
    async def scenario():
        ps = asyncio.create_task(server.call_tool('d365_models', {}))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            result = await asyncio.wait_for(server.call_tool('xref_names', {'name': 'ContosoBase'}), 2)
            assert not result.is_error and data(result)['names']
            assert not ps.done()
        finally:
            release.set()
            await ps
    asyncio.run(scenario())


def test_xref_unavailable_does_not_disable_d365(fake_ps, tmp_path):  # noqa: F811
    def unavailable():
        raise OSError('synthetic DB unavailable')
    server = ready_server(fake_ps, tmp_path, xref_index=xref.Xref(unavailable))
    async def scenario():
        failed = await server.call_tool('xref_info', {})
        assert failed.is_error and data(failed)['code'] == 'unavailable'
        status = await server.call_tool('d365_status', {})
        assert not status.is_error and data(status)['elevated']
    try:
        asyncio.run(scenario())
    finally:
        server._d365_close()


def test_combined_stdio_initialize_list_call(index, tmp_path):  # noqa: F811
    # index fixture writes this DB. The subprocess gets an explicit synthetic entry point.
    params = StdioServerParameters(command=sys.executable,
        args=[str(Path(__file__).with_name('combined_stdio_fixture.py')),
              str(tmp_path / 'xref.db'), str(tmp_path / 'state')], env=dict(os.environ))
    async def scenario():
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=5) as client:
                await client.initialize()
                tools = (await client.list_tools()).tools
                assert len(tools) == 13 and len({t.name for t in tools}) == 13
                result = await client.call_tool('xref_names', {'name': 'ContosoBase'})
                assert not result.is_error and data(result)['names']
                result = await client.call_tool('d365_status', {})
                assert not result.is_error and 'session' in data(result)
    asyncio.run(scenario())


def test_combined_http_discovery_calls_and_auth(index, tmp_path):  # noqa: F811
    class Combined(Service):
        def make_server(self, auth):
            return create_server(state_dir=self.tmp_path, start_session=False, xref_index=index,
                                 on_discover=self.watchdog.pinged, **auth)
    async def scenario():
        async with Combined(tmp_path) as service:
            body = {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {'_meta': _meta()}}
            response = await service.client.post('/mcp', json=body, headers=_headers('tools/list', token='pond'))
            assert response.status_code == 200, response.text
            tools = response.json()['result']['tools']
            assert len(tools) == 13
            refs = [t for t in tools if t['name'].startswith('xref_')]
            assert len(refs) == 4 and all(t['annotations']['readOnlyHint'] for t in refs)
            error, result = await service.result('xref_names', {'name': 'ContosoBase'})
            assert not error and result['names']
            error, result = await service.result('d365_status', {})
            assert not error and 'session' in result
            for token in (None, 'garden'):
                assert (await service.call('xref_info', {}, token)).status_code == 401
                assert (await service.call('d365_sql', {'sql': 'DROP TABLE forbidden'}, token)).status_code == 401
    asyncio.run(scenario())


def test_all_tool_schemas_and_annotations_match_pre_merge_contract(index, tmp_path):  # noqa: F811
    baseline = json.loads(Path(__file__).with_name('d365_xref_contract.json').read_text())
    server = create_server(state_dir=tmp_path, start_session=False, xref_index=index)
    tools = asyncio.run(server.list_tools())
    actual = {t.name: t.model_dump(exclude_none=True) for t in tools}
    # Keep the historical fixture intact. Only this explicit, reviewed delta is allowed.
    job = actual.pop("d365_job")
    old_job = baseline.pop("d365_job")
    assert actual == baseline
    from boring_service_kit.tools import WRITES
    assert job["annotations"] == WRITES.model_dump(exclude_none=True)
    wait = job["input_schema"]["properties"].pop("wait_seconds")
    assert wait["default"] == 0 and wait["type"] == "number"
    assert job["input_schema"] == old_job["input_schema"]
    for key in ("description", "annotations"):
        job.pop(key)
        old_job.pop(key)
    assert job == old_job


def test_lazy_xref_keeps_source_identity_on_success_and_failure(index, tmp_path, monkeypatch):  # noqa: F811
    captured = {}
    from boring_pondnt import d365_server
    original = d365_server.register_xref_tools
    def capture(server, query, log, guard, call_budget=None):
        captured['query'] = query
        original(server, query, log, guard, call_budget)
    monkeypatch.setattr(d365_server, 'register_xref_tools', capture)
    calls = []
    def connector_factory(*args):
        calls.append(args)
        def connect():
            return index._connect()
        connect.description = 'ODBC Driver 17 for SQL Server; ReferenceSQL; CustomReferences'
        return connect
    monkeypatch.setattr(xref, 'odbc_connector', connector_factory)
    server = create_server(state_dir=tmp_path, start_session=False,
                           xref_server='ReferenceSQL', xref_database='CustomReferences')
    assert not calls  # driver lookup is still deferred past discovery
    query = captured['query']
    query.sql = index.sql  # same query behavior, using the synthetic SQLite fixture
    result = asyncio.run(server.call_tool('xref_info', {}))
    assert not result.is_error
    assert data(result)['source'] == 'ODBC Driver 17 for SQL Server; ReferenceSQL; CustomReferences'
    assert calls == [('ReferenceSQL', 'CustomReferences', None, xref.STALL_SECONDS_DEFAULT)]
    def broken(*args):
        raise ImportError('synthetic missing driver')
    monkeypatch.setattr(xref, 'odbc_connector', broken)
    server = create_server(state_dir=tmp_path, start_session=False,
                           xref_server='OfflineSQL', xref_database='OtherReferences')
    result = asyncio.run(server.call_tool('xref_info', {}))
    assert result.is_error and data(result)['code'] == 'unavailable'
    assert 'OfflineSQL; OtherReferences' in data(result)['details']['source']
