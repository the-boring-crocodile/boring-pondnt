"""Operator settings determine the installation; planning never changes them."""
import json
from pathlib import Path

import pytest

from boring_pondnt import setup_services as setup


def run(state, servers=None, selected=("knowledge", "canvas"), apply=True):
    return setup.plan(servers or {}, "C:/Program Files/pondNT/candidate", str(state), "", list(selected),
                      "Robin", "BOX", apply=apply)


def entries(result):
    return {s["name"]: s["entry"] for s in result["services"] if "entry" in s}


def test_operator_config_bytes_are_kept_and_one_listener_follows_each_url(tmp_path):
    initial = run(tmp_path)
    first = entries(initial)
    initial_canvas = next(item for item in initial["services"] if item["name"] == "boring-canvas")
    config = tmp_path / "services/knowledge/config.json"
    operator = json.loads(config.read_text())
    operator.update(port=18612, url="http://127.0.0.1:18612/mcp")
    raw = (json.dumps(operator, indent=4) + "\n").encode()
    config.write_bytes(raw)
    canvas = tmp_path / "services/canvas/config.json"
    value = json.loads(canvas.read_text())
    value.update(port=18611, url="http://127.0.0.1:18611/drawing/mcp")
    canvas_raw = (json.dumps(value, indent=4) + "\n").encode()
    canvas.write_bytes(canvas_raw)
    result = run(tmp_path, first)
    items = {s["name"]: s for s in result["services"]}
    assert config.read_bytes() == raw and canvas.read_bytes() == canvas_raw
    assert items["boring-knowledge"]["entry"]["url"] == operator["url"]
    assert items["boring-canvas"]["url"] == value["url"]
    assert items["boring-canvas"]["arguments"] == initial_canvas["arguments"]
    # A second edit is still ours by its held credential after leaving the table port.
    operator.update(port=18712, url="http://127.0.0.1:18712/mcp")
    config.write_text(json.dumps(operator))
    again = run(tmp_path, entries(result))
    assert not again["conflicts"]
    assert entries(again)["boring-knowledge"]["url"] == operator["url"]
    retired = setup.uninstall(entries(again), str(tmp_path), installed=("knowledge", "canvas", "gateway"))
    canvas_item = next(item for item in retired["services"] if item["name"] == "boring-canvas")
    assert canvas_item["arguments"] == initial_canvas["arguments"]
    assert canvas_item["url"] == value["url"]


def test_old_secondary_canvas_port_is_free_for_another_service(tmp_path):
    first = entries(run(tmp_path))
    config = tmp_path / "services/knowledge/config.json"
    value = json.loads(config.read_text())
    value.update(port=18510, url="http://127.0.0.1:18510/mcp")
    config.write_text(json.dumps(value))
    assert entries(run(tmp_path, first))["boring-knowledge"]["url"] == value["url"]


@pytest.mark.parametrize("problem", ["duplicate-mcp", "unreadable", "mismatched-url"])
def test_bad_existing_config_refuses_before_any_other_file_changes(tmp_path, problem):
    first = entries(run(tmp_path))
    config = tmp_path / "services/knowledge/config.json"
    value = json.loads(config.read_text())
    if problem == "unreadable":
        config.write_text("{broken")
    else:
        port = {"duplicate-mcp": 18511, "mismatched-url": 18612}[problem]
        value["port"] = port
        if problem != "mismatched-url":
            value["url"] = f"http://127.0.0.1:{port}/mcp"
        config.write_text(json.dumps(value))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="config.json"):
        run(tmp_path, first)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_plan_checks_unselected_installed_listeners_too(tmp_path):
    run(tmp_path, selected=("canvas",))
    folder = tmp_path / "services/knowledge"
    folder.mkdir()
    config = folder / "config.json"
    config.write_text(json.dumps({"url": "http://127.0.0.1:18511/mcp", "port": 18511,
                                  "mode": "keys", "keys": {"file": "keys.json"}}))
    with pytest.raises(ValueError, match="also used"):
        run(tmp_path, selected=("knowledge",), apply=False)


def test_generated_gateway_routes_follow_operator_port(tmp_path):
    result = run(tmp_path, selected=("canvas",))
    config = tmp_path / "services/canvas/config.json"
    value = json.loads(config.read_text())
    value.update(port=18611, url="http://127.0.0.1:18611/mcp")
    config.write_text(json.dumps(value))
    run(tmp_path, entries(result), selected=("canvas",))
    routes = json.loads((tmp_path / "services/gateway/ferry.json").read_text())
    assert routes["backends"]["canvas"]["upstream"]["url"] == value["url"]


def test_every_service_uses_pythonw_and_only_command_servers_need_elevation(tmp_path):
    elevated = {"core", "d365", "d365whsapp", "d365web", "d365data"}
    for short, service in setup.SERVICES.items():
        item = setup.describe(service, "C:/Program Files/pondNT/candidate", str(tmp_path), "", "robin@box")
        assert item["account"] == "user" and item["python"].endswith("pythonw.exe")
        assert item["highest"] == (short in elevated)
