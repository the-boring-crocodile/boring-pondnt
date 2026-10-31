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
    assert len(answer["services"]) == 6  # the core, D365 and the gateway (retired: nothing behind it) too
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


def test_d365whsapp_is_opt_in_and_needs_its_config(tmp_path):
    from boring_pondnt.setup_services import DEFAULT_ONLY
    assert "d365whsapp" not in DEFAULT_ONLY.split(",")
    assert SERVICES["d365whsapp"].port == 18515 and SERVICES["d365whsapp"].account == "user"
    result = run({}, tmp_path, only=["d365whsapp"], apply=False)
    assert not result["services"] and "no d365whsapp.json" in result["conflicts"][0]
    folder = tmp_path / "services" / "d365whsapp"
    folder.mkdir(parents=True)
    (folder / "d365whsapp.json").write_text("{}")
    result = run({}, tmp_path, only=["d365whsapp"], apply=False)
    (item,) = result["services"]
    assert item["arguments"] == ["serve", "--config", str(folder / "config.json"), "--d365whsapp", str(folder / "d365whsapp.json")]
    assert item["url"] == "http://127.0.0.1:18515/mcp"


def test_d365web_is_opt_in_and_needs_its_config(tmp_path):
    from boring_pondnt.setup_services import DEFAULT_ONLY
    assert "d365web" not in DEFAULT_ONLY.split(",")
    assert SERVICES["d365web"].port == 18516 and SERVICES["d365web"].account == "user"
    result = run({}, tmp_path, only=["d365web"], apply=False)
    assert not result["services"] and "no d365web.json" in result["conflicts"][0]
    folder = tmp_path / "services" / "d365web"
    folder.mkdir(parents=True)
    (folder / "d365web.json").write_text("{}")
    result = run({}, tmp_path, only=["d365web"], apply=False)
    (item,) = result["services"]
    assert item["arguments"] == ["serve", "--config", str(folder / "config.json"), "--d365web", str(folder / "d365web.json")]
    assert item["url"] == "http://127.0.0.1:18516/mcp"


# ---------------------------------------------------------------- the gateway (D41)

GATED = ["sourcecache", "canvas", "d365"]


def gated_run(servers, tmp_path, gated=GATED, only=ALL + ["core", "d365"], now=NOW):
    (tmp_path / "handbook").mkdir(exist_ok=True)
    (tmp_path / "handbook" / "d365.md").write_text("# d365\n")
    result = plan(servers, "C:/tools/rel", str(tmp_path), "G:/pondnt/d365.db", only, "Robin", "DEVBOX",
                  apply=True, now=now, gated=gated)
    published = {k: v for k, v in servers.items() if k not in result["removals"]}
    published.update({s["name"]: s["entry"] for s in result["services"] if "entry" in s})
    finalize(published, str(tmp_path), "Robin", "DEVBOX")
    return result, published


def header(tmp_path, short):
    return json.loads((tmp_path / "services" / "gateway" / f"{short}.headers.json").read_text())


def test_gated_services_get_headers_not_entries_and_the_gateway_one_entry(tmp_path):
    result, published = gated_run({}, tmp_path)
    assert set(published) == {"boring-knowledge", "boring-pondnt", "boring-gateway"}
    items = {s["name"]: s for s in result["services"]}
    assert items["boring-gateway"]["arguments"][:2] == ["http", "--config"]
    assert items["boring-gateway"]["module"] == "boring_mcp_ferry.server"
    assert items["boring-gateway"]["account"] == "system" and items["boring-gateway"]["url"].endswith(":18517/mcp")
    assert result["gateway"]["carries"] == ["boring-sourcecache", "boring-canvas", "boring-d365"]
    for short in GATED:
        item = items[SERVICES[short].name]
        assert item["behind"] == "boring-gateway" and "entry" not in item
        key = header(tmp_path, short)["Authorization"].removeprefix("Bearer ")
        config = ServiceConfig(tmp_path / "services" / short / "config.json")
        assert asyncio.run(config.keys.verify_token(key)).subject == "robin@devbox"
    gateway = ServiceConfig(tmp_path / "services" / "gateway" / "config.json")
    assert gateway.bind == "127.0.0.1" and gateway.verifier is None
    key = published["boring-gateway"]["headers"]["Authorization"].removeprefix("Bearer ")
    assert asyncio.run(gateway.keys.verify_token(key)).subject == "robin@devbox"


def test_the_ferry_reads_the_written_configuration(tmp_path):
    ferry = __import__("pytest").importorskip("boring_mcp_ferry.config")
    gated_run({}, tmp_path)
    read = ferry.read(tmp_path / "services" / "gateway" / "ferry.json")
    assert [b.name for b in read.backends] == GATED
    assert [(g.name, g.backends) for g in read.gateways] == [("pondnt", tuple(GATED))]
    by_name = {b.name: b for b in read.backends}
    assert by_name["d365"].guide == "# d365\n" and by_name["canvas"].guide is None
    assert by_name["d365"].url == "http://127.0.0.1:18514/mcp" and by_name["d365"].service is None
    assert by_name["d365"].headers["Authorization"].startswith("Bearer ")


def test_moving_direct_services_behind_the_gateway_removes_their_entries_and_old_keys(tmp_path):
    direct = entries(run({}, tmp_path, only=ALL + ["core", "d365"]))
    result, published = gated_run(direct, tmp_path)
    assert sorted(result["removals"]) == ["boring-canvas", "boring-d365", "boring-sourcecache"]
    for short in GATED:
        loaded = keys.KeyFile.load(tmp_path / "services" / short / "keys.json")
        assert len(loaded.keys) == 1  # the entry's key retired after publication
        assert loaded.keys[0].sha256 == keys.fingerprint(header(tmp_path, short)["Authorization"][7:])


def test_a_rerun_keeps_the_gated_keys(tmp_path):
    _, first = gated_run({}, tmp_path)
    before = {s: header(tmp_path, s) for s in GATED}
    result, again = gated_run(first, tmp_path)
    assert {s: header(tmp_path, s) for s in GATED} == before and again == first
    assert not any(s["new_key"] or s["entry_changes"] for s in result["services"]) and not result["removals"]


def test_gateway_none_retires_it_and_brings_the_services_back(tmp_path):
    _, published = gated_run({}, tmp_path)
    result, again = gated_run(published, tmp_path, gated=[])
    assert result["removals"] == ["boring-gateway"]
    assert [r["name"] for r in result["retire"]] == ["boring-gateway"]
    assert {"boring-sourcecache", "boring-canvas", "boring-d365"} <= set(again)
    folder = tmp_path / "services" / "gateway"
    assert not (folder / "ferry.json").exists() and not list(folder.glob("*.headers.json"))
    for short in GATED:
        assert len(keys.KeyFile.load(tmp_path / "services" / short / "keys.json").keys) == 1


def test_choose_gated(tmp_path):
    from boring_pondnt.setup_services import choose_gated
    only = ["sourcecache", "knowledge", "core", "d365"]
    assert choose_gated("none", only, str(tmp_path)) == (only, [])
    assert choose_gated("all", only, str(tmp_path)) == (only, ["sourcecache", "d365"])
    assert choose_gated("d365", only, str(tmp_path)) == (only, ["d365"])
    assert choose_gated("keep", only, str(tmp_path)) == (only, [])
    for wrong in ("core", "knowledge", "canvas", "gateway"):
        with __import__("pytest").raises(ValueError):
            choose_gated(wrong, only, str(tmp_path))
    gated_run({}, tmp_path)  # canvas behind the gateway now: keep sets it up again even when not chosen
    assert choose_gated("keep", only, str(tmp_path)) == (only + ["canvas"], GATED)


def test_uninstall_finds_the_gated_services_and_the_gateway(tmp_path):
    _, published = gated_run({}, tmp_path)
    answer = uninstall(published, str(tmp_path), "C:/tools/rel", "G:/pondnt/d365.db")
    assert sorted(answer["removals"]) == ["boring-gateway", "boring-knowledge", "boring-pondnt"]
    assert set(answer["tasks"]) == {f"pondNT {SERVICES[s].name}" for s in ALL + ["core", "d365", "gateway"]}
    assert {s["name"] for s in answer["services"]} == {SERVICES[s].name for s in ALL + ["core", "d365", "gateway"]}



def test_gateway_none_restores_previously_gated_opt_in_services(tmp_path):
    from boring_pondnt.setup_services import choose_gated
    folder = tmp_path / "services" / "d365web"
    folder.mkdir(parents=True)
    (folder / "d365web.json").write_text("{}")
    _, published = gated_run({}, tmp_path, gated=["d365web"], only=["core", "knowledge", "d365web"])
    only, gated = choose_gated("none", ["core", "knowledge"], str(tmp_path))
    assert only == ["core", "knowledge", "d365web"] and not gated
    result, published = gated_run(published, tmp_path, gated=gated, only=only)
    assert "boring-d365web" in published and "boring-gateway" not in published


def test_custom_gateway_prevents_transition_without_mutation(tmp_path):
    custom = {"boring-gateway": {"type": "http", "url": "https://custom.example/mcp"}}
    result = plan(custom, "C:/tools/rel", str(tmp_path), "", ["canvas"], "Robin", "DEVBOX",
                  apply=True, now=NOW, gated=["canvas"])
    assert result["conflicts"] and not result["services"] and not result["removals"]
    assert not list(tmp_path.iterdir())


def test_uninstall_preserves_custom_entries_even_with_gated_state(tmp_path):
    _, published = gated_run({}, tmp_path)
    published["boring-gateway"] = {"type": "http", "url": "https://custom.example/gateway"}
    published["boring-d365"] = {"type": "http", "url": "https://custom.example/d365"}
    answer = uninstall(published, str(tmp_path), "C:/tools/rel", "G:/pondnt/d365.db")
    assert "boring-gateway" not in answer["removals"]
    assert "boring-d365" not in answer["removals"]


def test_corrupt_gateway_state_does_not_become_direct_mode(tmp_path):
    import pytest
    from boring_pondnt.setup_services import choose_gated
    folder = tmp_path / "services" / "gateway"
    folder.mkdir(parents=True)
    (folder / "ferry.json").write_text('{"backends": []}')
    with pytest.raises(ValueError, match="Invalid installed gateway"):
        choose_gated("keep", ["core"], str(tmp_path))


def test_the_gateway_is_never_chosen_with_only(tmp_path, monkeypatch, capsys):
    import io
    import pytest
    from boring_pondnt import setup_services
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    with pytest.raises(SystemExit):
        setup_services.main(["--root", "C:/r", "--state", str(tmp_path), "--only", "gateway", "--plan"])
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    setup_services.main(["--root", "C:/r", "--state", str(tmp_path), "--index", "G:/x.db", "--gateway", "all",
                         "--plan"])
    answer = json.loads(capsys.readouterr().out)
    assert [s["name"] for s in answer["services"]][-1] == "boring-gateway"
    assert {s["name"] for s in answer["services"] if s["behind"]} == {"boring-sourcecache", "boring-canvas",
                                                                      "boring-d365"}
    assert not list(tmp_path.iterdir())
