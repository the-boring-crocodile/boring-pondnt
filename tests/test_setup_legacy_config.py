"""Upgrade the desktop state written by 0.2.10 and kept by internal 0.3.0."""
import json

import pytest

from boring_mcp_ferry.server import validate as validate_ferry
from boring_pondnt import setup_services as setup
from boring_service_kit import keys
from boring_service_kit.http import ServiceConfig

NOW = 1_800_000_000
DIRECT = {"core", "knowledge", "gateway"}


def snapshot(state):
    return {p: p.read_bytes() for p in state.rglob("*") if p.is_file()}


def legacy_state(state):
    servers, configs, bearers, backends = {}, {}, {}, {}
    for short, service in setup.SERVICES.items():
        folder = state / "services" / short
        folder.mkdir(parents=True, exist_ok=True)
        config = {"url": service.url, "bind": "127.0.0.1", "port": service.port, "keys": {"file": "keys.json"}}
        (folder / "config.json").write_text(json.dumps(config), encoding="utf-8")
        configs[short] = config
        _, bearer = keys.add(folder / "keys.json", "robin@box", "writer", 365,
                             setup.LABEL, issuer=f"key:box-{short}", now=NOW)
        headers = {"Authorization": "Bearer " + bearer}
        bearers[short] = headers
        if short in DIRECT:
            servers[service.name] = {"type": "http", "url": service.url, "headers": headers}
        else:
            (state / "services/gateway").mkdir(exist_ok=True)
            (state / "services/gateway" / f"{short}.headers.json").write_text(json.dumps(headers), encoding="utf-8")
            backends[short] = {"upstream": {"url": service.url, "headers_file": f"{short}.headers.json",
                                            "timeout_seconds": 900}}
        if short in setup.OPT_IN_CONFIGS:
            (folder / setup.OPT_IN_CONFIGS[short][0]).write_text("{}", encoding="utf-8")
    (state / "services/canvas/web.json").write_text(
        '{"port":18510,"public_base_url":"http://127.0.0.1:18510"}', encoding="utf-8")
    gateway = state / "services/gateway"
    (gateway / "selection.json").write_text(json.dumps({"backends": list(backends)}), encoding="utf-8")
    (gateway / "ferry.json").write_text(json.dumps({
        "backends": backends, "gateways": {"pondnt": {"service": "config.json", "backends": list(backends)}},
    }), encoding="utf-8")
    return servers, configs, bearers


def upgrade(state, servers, *, apply):
    return setup.plan(servers, "C:/release", str(state), "G:/index.db", [],
                      "Robin", "BOX", apply=apply, now=NOW + keys.DAY, installed=tuple(setup.SERVICES))


def test_legacy_describe_preview_and_apply_reach_the_end(tmp_path, capsys):
    servers, configs, bearers = legacy_state(tmp_path)
    before = snapshot(tmp_path)
    setup.main(["--root", "C:/release", "--state", str(tmp_path), "--describe"])
    assert {item["short"] for item in json.loads(capsys.readouterr().out)["services"]} == set(configs)

    preview = upgrade(tmp_path, servers, apply=False)
    assert not preview["conflicts"] and not preview["removals"] and snapshot(tmp_path) == before
    assert len(preview["services"]) == 9
    assert {item["config_change"] for item in preview["services"]} == {"config.json: adds mode=keys"}
    assert not any(item["new_key"] or item["entry_changes"] for item in preview["services"])

    applied = upgrade(tmp_path, servers, apply=True)
    assert [item["name"] for item in applied["services"]] == [item["name"] for item in preview["services"]]
    assert {s["name"]: s["entry"] for s in applied["services"] if "entry" in s} == servers
    for short, config in configs.items():
        folder = tmp_path / "services" / short
        path = folder / "config.json"
        assert json.loads(path.read_bytes()) == {**config, "mode": "keys"}
        runtime = ServiceConfig(path)
        assert (runtime.mode, runtime.bind, runtime.port) == ("keys", "127.0.0.1", config["port"])
        assert json.loads((folder / "client.headers.json").read_bytes()) == bearers[short]
    gateway = tmp_path / "services/gateway"
    routes = json.loads((gateway / "ferry.json").read_bytes())
    assert set(routes["backends"]) == set(configs) - DIRECT
    validate_ferry(gateway / "ferry.json")
    canvas = next(item for item in applied["services"] if item["name"] == "boring-canvas")
    assert canvas["url"] == "http://127.0.0.1:18511/mcp" and "--port" not in canvas["arguments"]
    for old_sidecar in (gateway / "selection.json", tmp_path / "services/canvas/web.json"):
        assert old_sidecar.read_bytes() == before[old_sidecar]
    migrated = snapshot(tmp_path)
    rerun = upgrade(tmp_path, servers, apply=True)
    assert not any(item.get("config_change") for item in rerun["services"])
    assert snapshot(tmp_path) == migrated


@pytest.mark.parametrize("problem", ["missing-bind", "extra-key", "wrong-keys", "wrong-port", "non-loopback", "mode-null"])
def test_nonlegacy_or_invalid_config_refuses_the_whole_scope(tmp_path, problem):
    servers, configs, _ = legacy_state(tmp_path)
    value = configs["gateway"]
    if problem == "missing-bind":
        value.pop("bind")
    elif problem == "extra-key":
        value["behind_proxy"] = False
    elif problem == "wrong-keys":
        value["keys"]["extra"] = "no"
    elif problem == "wrong-port":
        value["port"] += 1
    elif problem == "non-loopback":
        value["bind"] = "0.0.0.0"
    else:
        value["mode"] = None
    (tmp_path / "services/gateway/config.json").write_text(json.dumps(value), encoding="utf-8")
    before = snapshot(tmp_path)
    for apply in (False, True):
        with pytest.raises(ValueError, match=r"gateway[/\\]config\.json"):
            upgrade(tmp_path, servers, apply=apply)
        assert snapshot(tmp_path) == before
