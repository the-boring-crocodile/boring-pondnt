"""Local services with access keys: plan, apply, keep or rotate keys, uninstall. Synthetic configs only."""
import asyncio
import json

from boring_service_kit import keys
from boring_service_kit.http import ServiceConfig
from boring_pondnt.setup_services import LABEL, SERVICES, plan, uninstall, finalize

NOW = 1_800_000_000.0
ALL = ["sourcecache", "knowledge", "canvas"]


def run(servers, tmp_path, only=ALL, apply=True, now=NOW, index="G:/pondnt/d365.db"):
    result = plan(servers, "C:/tools/rel", str(tmp_path), index, only, "Robin", "DEVBOX", apply=apply, now=now)
    if apply:
        finalize(entries(result), str(tmp_path), "Robin", "DEVBOX")
    return result


def entries(result):
    return {s["name"]: s["entry"] for s in result["services"]}


def test_plan_only_writes_nothing(tmp_path):
    result = run({}, tmp_path, apply=False)
    assert [s["name"] for s in result["services"]] == ["boring-sourcecache", "boring-knowledge", "boring-canvas"]
    assert all("entry" not in s for s in result["services"]) and not list(tmp_path.iterdir())


def test_apply_writes_configs_that_load_and_keys_that_verify(tmp_path):
    result = run({}, tmp_path)
    for item, (short, service) in zip(result["services"], SERVICES.items()):
        folder = tmp_path / "services" / short
        config = ServiceConfig(folder / "config.json")
        assert config.url == service.url and config.bind == "127.0.0.1" and config.verifier is None
        key = item["entry"]["headers"]["Authorization"].removeprefix("Bearer ")
        assert key not in (folder / "keys.json").read_text()
        access = asyncio.run(config.keys.verify_token(key))
        assert access.subject == "robin@devbox"
        assert access.claims["boring_caller"]["role"] == "writer"
        assert access.claims["boring_caller"]["issuer"] == f"key:devbox-{short}"
        assert item["entry"]["type"] == "http" and item["entry"]["url"] == service.url
        assert item["task"] == f"pondNT {service.name}" and item["new_key"]


def test_rerun_keeps_a_valid_key_and_changes_no_entry(tmp_path):
    first = entries(run({}, tmp_path))
    second = run(first, tmp_path)
    assert entries(second) == first
    assert not any(s["entry_changes"] or s["new_key"] for s in second["services"])
    for short in ALL:
        assert len(keys.KeyFile.load(tmp_path / "services" / short / "keys.json").keys) == 1


def test_key_near_expiry_or_lost_is_rotated_and_old_setup_key_removed(tmp_path):
    first = entries(run({}, tmp_path))
    later = NOW + 340 * keys.DAY  # 25 days left
    second = run(first, tmp_path, now=later)
    assert all(s["new_key"] and s["entry_changes"] for s in second["services"])
    keyfile = tmp_path / "services" / "knowledge" / "keys.json"
    assert len(keys.KeyFile.load(keyfile).keys) == 1
    old = first["boring-knowledge"]["headers"]["Authorization"].removeprefix("Bearer ")
    config = ServiceConfig(tmp_path / "services" / "knowledge" / "config.json")
    assert asyncio.run(config.keys.verify_token(old)) is None


def test_admin_keys_in_the_same_file_survive_rotation(tmp_path):
    run({}, tmp_path)
    keyfile = tmp_path / "services" / "knowledge" / "keys.json"
    admin, admin_key = keys.add(keyfile, "colleague@vm", "reader", 30, "by hand", now=NOW)
    run({}, tmp_path)  # entries lost: rotate our key
    loaded = keys.KeyFile.load(keyfile)
    assert admin in loaded.keys and sum(k.label == LABEL for k in loaded.keys) == 1


def test_customized_entries_are_left_alone(tmp_path):
    servers = {"boring-knowledge": {"type": "http", "url": "https://knowledge.example/mcp"},
               "boring-sourcecache": {"type": "stdio", "command": "C:/tools/rc1/boring-sourcecache-d365.cmd",
                                      "args": ["serve", "--db", "G:/pondnt/d365.db"]}}
    result = run(servers, tmp_path)
    assert [s["name"] for s in result["services"]] == ["boring-canvas"]
    assert sorted(result["conflicts"]) == ["boring-knowledge: customized entry retained; not configured",
                                           "boring-sourcecache: customized entry retained; not configured"]


def test_no_index_no_sourcecache(tmp_path):
    result = run({}, tmp_path, index="")
    assert "boring-sourcecache" not in entries(result)
    assert result["conflicts"] == ["boring-sourcecache: no source index; build it first"]


def test_service_arguments(tmp_path):
    items = {s["name"]: s for s in run({}, tmp_path, apply=False)["services"]}
    assert items["boring-sourcecache"]["arguments"][:2] == ["serve", "--config"]
    assert "--db" in items["boring-knowledge"]["arguments"] and items["boring-knowledge"]["arguments"][0] == "http"
    canvas = items["boring-canvas"]["arguments"]
    assert canvas[canvas.index("--host") + 1] == "127.0.0.1" and "--public-base-url" in canvas
    assert "--unsafe-url-auth" in canvas  # else a person never gets the canvas token over MCP


def test_uninstall_lists_only_owned_entries(tmp_path):
    servers = entries(run({}, tmp_path))
    servers["boring-canvas"] = {"type": "http", "url": "https://canvas.example/mcp"}
    answer = uninstall(servers, str(tmp_path))
    assert answer["removals"] == ["boring-sourcecache", "boring-knowledge"]
    assert answer["conflicts"] == ["boring-canvas: customized entry retained"]
    assert answer["tasks"] == ["pondNT boring-sourcecache", "pondNT boring-knowledge"]
    assert all("canvas" not in folder for folder in answer["folders"])
    assert [s["name"] for s in answer["services"]] == ["boring-knowledge"]
    assert json.dumps(answer)  # Sourcecache task needs explicit index to prove its command.


def test_uninstall_without_owned_entries_targets_nothing(tmp_path):
    answer = uninstall({"boring-canvas": {"type": "http", "url": "https://custom/mcp"}}, str(tmp_path))
    assert not answer["tasks"] and not answer["folders"] and not answer["services"]


def test_uninstall_task_specs_reuse_service_arguments_without_writes(tmp_path):
    servers = {s.name: {"type": "http", "url": s.url} for s in SERVICES.values()}
    answer = uninstall(servers, str(tmp_path), "C:/release", "G:/index.db")
    assert len(answer["services"]) == 5  # the core and D365 too
    assert not list(tmp_path.iterdir())
    assert all("entry" not in s for s in answer["services"])


def test_command_line_plan_runs_under_the_shipped_python(tmp_path, monkeypatch, capsys):
    # 0.2.0rc2 on the VM: Python 3.14 rejects a "%" in an argparse help string when the argument is added.
    import io
    from boring_pondnt import setup_services
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    setup_services.main(["--root", "C:/tools/rel", "--state", str(tmp_path), "--index", "G:/x.db", "--plan"])
    answer = json.loads(capsys.readouterr().out)
    assert [s["name"] for s in answer["services"]] == ["boring-sourcecache", "boring-knowledge", "boring-canvas",
                                                       "boring-pondnt", "boring-d365"]  # core and D365


def test_core_runs_over_http_as_the_person(tmp_path):
    """The core is an HTTP entry with a key; its task is the person's at logon (Copilot's
    commands run as them), the services' stay SYSTEM at boot."""
    result = run({}, tmp_path, only=ALL + ["core"])
    core = next(s for s in result["services"] if s["name"] == "boring-pondnt")
    assert core["account"] == "user" and core["module"] == "boring_pondnt.server"
    assert core["python"].endswith("pythonw.exe")  # no console window in the person's session
    assert core["arguments"][:1] == ["--config"] and "--state-dir" not in core["arguments"]
    assert core["entry"]["type"] == "http" and core["entry"]["url"] == "http://127.0.0.1:18513/mcp"
    assert core["entry"]["headers"]["Authorization"].startswith("Bearer ")
    assert {s["account"] for s in result["services"] if s["name"] != "boring-pondnt"} == {"system"}
    config = ServiceConfig(tmp_path / "services" / "core" / "config.json")
    assert config.call_limit is None  # loopback for Copilot: no Pond bound


def test_a_customized_core_entry_is_left_alone(tmp_path):
    custom = {"boring-pondnt": {"type": "stdio", "command": "C:/tools/rc12/boring-pondnt-core.cmd",
                                "args": ["--kit", "D:/my-kit"]}}
    result = run(custom, tmp_path, only=["core"], apply=False)
    assert not result["services"] and any("boring-pondnt" in c for c in result["conflicts"])


def test_local_ports_stay_clear_of_the_pond_tunnel():
    """tools/devbox-start.ps1 forwards 18520 (core) and 18522 (D365) for the Pond; the local
    services and the core must never take them (found while adding the core)."""
    ports = [s.port for s in SERVICES.values()]
    assert len(set(ports)) == len(ports) and not {18520, 18522, 18503} & set(ports)
    assert SERVICES["d365"].port == 18514


def test_d365_runs_over_http_as_the_person(tmp_path):
    """The D365 server is an HTTP entry with a key; the task is the person's, elevated."""
    result = run({}, tmp_path, only=["d365"])
    d365 = result["services"][0]
    assert d365["account"] == "user" and d365["python"].endswith("pythonw.exe")
    assert d365["module"] == "boring_pondnt.d365_server" and d365["arguments"][:2] == ["serve", "--config"]
    assert d365["entry"]["url"] == "http://127.0.0.1:18514/mcp"
    assert ServiceConfig(tmp_path / "services" / "d365" / "config.json").call_limit is None


def test_a_d365_entry_with_its_own_options_is_left_alone(tmp_path):
    custom = {"boring-d365": {"type": "stdio", "command": "C:/tools/rc15/boring-pondnt-d365.cmd",
                              "args": ["serve", "--sql-server", "ax2"]}}
    result = run(custom, tmp_path, only=["d365"], apply=False)
    assert not result["services"] and any("boring-d365" in c for c in result["conflicts"])
