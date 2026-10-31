"""Interrupted routing changes retain the previous client's usable credentials and files."""
import asyncio
import json
import time

import pytest
from boring_service_kit.http import ServiceConfig
from boring_pondnt import setup_services as setup


def prepared(state, servers, gated, only=None):
    return setup.plan(servers, "C:/release", str(state), "", only or ["knowledge", "canvas", "d365web"],
                      "Robin", "DEVBOX", apply=True, now=time.time(), gated=gated)


def published(servers, result):
    value = {k: v for k, v in servers.items() if k not in result["removals"]}
    value.update({s["name"]: s["entry"] for s in result["services"] if "entry" in s})
    return value


def finish(state, servers, result):
    value = published(servers, result)
    # Finalization consumes the actual published shape, not the preparation result.
    path = state / "client.json"
    path.write_text(json.dumps({"servers": value}))
    setup.finalize(json.loads(path.read_text())["servers"], str(state), "Robin", "DEVBOX")
    return value


def existing(state):
    web = state / "services/d365web"
    web.mkdir(parents=True)
    (web / "d365web.json").write_text("{}")
    result = prepared(state, {}, ["canvas", "d365web"])
    return finish(state, {}, result)


def verify(state, short, authorization):
    config = ServiceConfig(state / "services" / short / "config.json")
    access = asyncio.run(config.keys.verify_token(authorization.removeprefix("Bearer ")))
    assert access.subject == "robin@devbox"


def test_interrupted_gateway_retirement_keeps_roster_and_old_credentials_for_retry(tmp_path):
    servers = existing(tmp_path)
    gateway = tmp_path / "services/gateway"
    before = {p.name: p.read_bytes() for p in gateway.glob("*.headers.json")}
    before["ferry.json"] = (gateway / "ferry.json").read_bytes()
    only, gated = setup.choose_gated("none", ["knowledge"], str(tmp_path))
    assert "d365web" in only
    prepared(tmp_path, servers, gated, only)  # interrupted before task/client publication
    assert all((gateway / n).read_bytes() == data for n, data in before.items())
    verify(tmp_path, "gateway", servers["boring-gateway"]["headers"]["Authorization"])
    for short in ("canvas", "d365web"):
        verify(tmp_path, short, json.loads(before[short + ".headers.json"])["Authorization"])

    # The same minimal retry still discovers the opt-in backend from retained state.
    retry_only, retry_gated = setup.choose_gated("none", ["knowledge"], str(tmp_path))
    assert retry_only == only
    result = prepared(tmp_path, servers, retry_gated, retry_only)
    new_client = finish(tmp_path, servers, result)
    assert "boring-gateway" not in new_client and "boring-d365web" in new_client
    assert not (gateway / "ferry.json").exists() and not list(gateway.glob("*.headers.json"))
    verify(tmp_path, "d365web", new_client["boring-d365web"]["headers"]["Authorization"])


def test_shrinking_gateway_keeps_retired_backend_headers_until_publication(tmp_path):
    servers = existing(tmp_path)
    gateway = tmp_path / "services/gateway"
    old_header = (gateway / "d365web.headers.json").read_bytes()
    result = prepared(tmp_path, servers, ["canvas"])
    assert (gateway / "d365web.headers.json").read_bytes() == old_header
    verify(tmp_path, "d365web", json.loads(old_header)["Authorization"])
    new_client = finish(tmp_path, servers, result)
    assert (gateway / "canvas.headers.json").exists()
    assert not (gateway / "d365web.headers.json").exists()
    verify(tmp_path, "d365web", new_client["boring-d365web"]["headers"]["Authorization"])


def test_failed_preparation_preserves_previous_gateway_and_client_keys(tmp_path, monkeypatch):
    servers = existing(tmp_path)
    gateway = tmp_path / "services/gateway"
    old_ferry = (gateway / "ferry.json").read_bytes()
    actual = setup.write_private

    def fail(path, value):
        if path.name == "d365web.headers.json":
            raise OSError("synthetic interrupted preparation")
        actual(path, value)

    monkeypatch.setattr(setup, "write_private", fail)
    with pytest.raises(OSError, match="interrupted preparation"):
        prepared(tmp_path, servers, ["canvas", "d365web"])
    assert (gateway / "ferry.json").read_bytes() == old_ferry
    verify(tmp_path, "gateway", servers["boring-gateway"]["headers"]["Authorization"])
    for short in ("canvas", "d365web"):
        verify(tmp_path, short, json.loads((gateway / (short + ".headers.json")).read_text())["Authorization"])


def test_customized_client_gateway_refuses_cleanup_and_preserves_routing(tmp_path):
    servers = existing(tmp_path)
    gateway = tmp_path / "services/gateway"
    before = {p.name: p.read_bytes() for p in gateway.iterdir() if p.is_file()}
    servers["boring-gateway"] = {"type": "http", "url": "https://custom.example/mcp"}
    with pytest.raises(ValueError, match="customized"):
        setup.finalize(servers, str(tmp_path), "Robin", "DEVBOX")
    assert {p.name: p.read_bytes() for p in gateway.iterdir() if p.is_file()} == before
