"""Source-level layout journey for Phase21; fixture files only."""
import json
import pytest
from boring_pondnt import setup_services as setup


def prepare(state, only=None, servers=None, apply=True):
    return setup.plan(servers or {}, "C:/reviewed-release", str(state), "G:/source.db",
                      only or setup.DEFAULT_ONLY.split(","), "Robin", "BOX", apply=apply)


def entries(result):
    return {item["name"]: item["entry"] for item in result["services"] if "entry" in item}


def test_fresh_layout_has_three_entries_without_an_operator_roster(tmp_path):
    result = prepare(tmp_path)
    assert set(entries(result)) == {"boring-pondnt", "boring-knowledge", "boring-gateway"}
    assert result["gateway"]["carries"] == ["boring-sourcecache", "boring-canvas", "boring-d365"]
    routes = json.loads((tmp_path / "services/gateway/ferry.json").read_text())
    assert set(routes["backends"]) == {"sourcecache", "canvas", "d365"}
    assert not (tmp_path / "services/gateway/selection.json").exists()
    assert not (tmp_path / "services/gateway/ferry.before-selection.json").exists()


def test_every_opt_in_specialist_uses_the_same_fixed_gateway_rule(tmp_path):
    for short, (filename, _) in setup.OPT_IN_CONFIGS.items():
        folder = tmp_path / "services" / short
        folder.mkdir(parents=True)
        (folder / filename).write_text("{}")
    chosen = setup.DEFAULT_ONLY.split(",") + list(setup.OPT_IN_CONFIGS)
    result = prepare(tmp_path, only=chosen)
    assert set(entries(result)) == {"boring-pondnt", "boring-knowledge", "boring-gateway"}
    for item in result["services"]:
        direct = item["name"] in {"boring-pondnt", "boring-knowledge", "boring-gateway"}
        assert (item["behind"] is None) is direct
    routes = json.loads((tmp_path / "services/gateway/ferry.json").read_text())
    assert set(routes["backends"]) == set(chosen) - {"core", "knowledge"}
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    prepare(tmp_path, only=chosen, servers=entries(result))
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_core_and_knowledge_only_need_no_gateway(tmp_path):
    result = prepare(tmp_path, only=["core", "knowledge"])
    assert set(entries(result)) == {"boring-pondnt", "boring-knowledge"}
    assert "gateway" not in result


def test_preview_lists_the_fixed_layout_and_writes_nothing(tmp_path):
    result = prepare(tmp_path, apply=False)
    assert "boring-gateway" in {item["name"] for item in result["services"]}
    assert all("entry" not in item for item in result["services"])
    assert not list(tmp_path.iterdir())


def test_owned_task_scope_keeps_backend_when_adding_a_direct_service(tmp_path):
    first = prepare(tmp_path, only=["canvas"])
    header = tmp_path / "services/canvas/client.headers.json"
    before = header.read_bytes()
    result = setup.plan(entries(first), "C:/reviewed-release", str(tmp_path), "",
                        ["knowledge"], "Robin", "BOX", apply=True,
                        installed=("canvas", "gateway"))
    assert result["gateway"]["carries"] == ["boring-canvas"]
    assert header.read_bytes() == before
    assert {s["name"] for s in result["services"]} == {
        "boring-knowledge", "boring-canvas", "boring-gateway"}


def test_retained_config_is_not_an_installed_task_roster(tmp_path):
    prepare(tmp_path, only=["canvas"])
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = setup.plan({}, "C:/reviewed-release", str(tmp_path), "", ["knowledge"],
                        "Robin", "BOX", apply=False, installed=())
    assert [s["name"] for s in result["services"]] == ["boring-knowledge"]
    assert "gateway" not in result
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_uninstall_uses_hidden_owned_tasks_and_the_observed_index(tmp_path):
    first = prepare(tmp_path)
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = setup.uninstall(entries(first), str(tmp_path), "C:/reviewed-release",
                             "G:/source.db", installed=tuple(setup.DEFAULT_ONLY.split(",") + ["gateway"]))
    assert len(result["tasks"]) == 6
    source = next(s for s in result["services"] if s["name"] == "boring-sourcecache")
    assert source["arguments"][source["arguments"].index("--db") + 1] == "G:/source.db"
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


def test_describe_reads_no_client_input_and_writes_nothing(tmp_path, monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("not client JSON"))
    setup.main(["--root", "C:/release", "--state", str(tmp_path), "--describe"])
    result = json.loads(capsys.readouterr().out)
    assert {s["short"] for s in result["services"]} == set(setup.SERVICES)
    source = next(s for s in result["services"] if s["short"] == "sourcecache")
    assert source["arguments"][source["arguments"].index("--db") + 1] == ""
    assert not list(tmp_path.iterdir())
