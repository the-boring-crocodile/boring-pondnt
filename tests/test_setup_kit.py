"""The kit lives in the state folder: seeded once, never overwritten, differences reported."""
import json
from pathlib import Path

from boring_pondnt.server import bootstrap_result
from boring_pondnt.setup_services import SERVICES, plan, uninstall


def release(tmp_path: Path, name: str, persona: str = "Fitzwilliam") -> Path:
    root = tmp_path / name
    (root / "kit").mkdir(parents=True)
    (root / "kit" / "bootstrap.md").write_text("# bootstrap\n", encoding="utf-8")
    (root / "kit" / "persona.md").write_text(f"{persona}\n", encoding="utf-8")
    return root


def setup(root: Path, state: Path, apply: bool = True) -> dict:
    return plan({}, str(root), str(state), "", ["core"], "Robin", "DEVBOX", apply=apply)


def test_first_install_seeds_the_state_kit_and_the_core_serves_it(tmp_path):
    root, state = release(tmp_path, "rc20"), tmp_path / "state"
    preview = setup(root, state, apply=False)
    assert preview["kit"]["state"] == "new" and not (state / "kit").exists()  # a plan writes nothing
    result = setup(root, state)
    assert result["kit"]["seeded"] and result["kit"]["differs"] == []
    assert (state / "kit" / "persona.md").read_text(encoding="utf-8") == "Fitzwilliam\n"
    core = result["services"][0]
    assert core["arguments"][core["arguments"].index("--kit") + 1] == str(state / "kit")
    assert not [p for p in state.iterdir() if p.name.startswith(".kit-")]  # no staging left


def test_a_later_release_never_overwrites_the_kit_and_reports_the_differences(tmp_path):
    state = tmp_path / "state"
    setup(release(tmp_path, "rc20"), state)
    (state / "kit" / "persona.md").write_text("Fitzwilliam, as the owner adapted him\n", encoding="utf-8")
    (state / "kit" / "project.md").write_text("our notes\n", encoding="utf-8")
    newer = release(tmp_path, "rc21", persona="Fitzwilliam, revised upstream")
    result = setup(newer, state)
    assert result["kit"]["state"] == "kept" and "seeded" not in result["kit"]
    assert result["kit"]["differs"] == ["persona.md", "project.md"]
    assert (state / "kit" / "persona.md").read_text(encoding="utf-8") == "Fitzwilliam, as the owner adapted him\n"


def test_a_kit_folder_without_bootstrap_is_left_alone_and_reported(tmp_path):
    state = tmp_path / "state"
    (state / "kit").mkdir(parents=True)
    result = setup(release(tmp_path, "rc20"), state)
    assert result["kit"]["state"] == "no-bootstrap" and not list((state / "kit").iterdir())
    assert any(c.startswith("kit: ") for c in result["conflicts"])


def test_uninstall_keeps_the_kit_and_names_it(tmp_path):
    root, state = release(tmp_path, "rc20"), tmp_path / "state"
    servers = {s["name"]: s["entry"] for s in setup(root, state)["services"]}
    answer = uninstall(servers, str(state), str(root))
    assert answer["kit"] == str(state / "kit") and (state / "kit" / "bootstrap.md").is_file()
    assert json.dumps(answer)


def test_bootstrap_names_the_release_kit_when_serving_another(tmp_path, monkeypatch):
    root, state = release(tmp_path, "rc20"), tmp_path / "state"
    setup(root, state)
    monkeypatch.setattr("boring_pondnt.server.default_kit", lambda: root / "kit")
    served = bootstrap_result(state / "kit")
    assert served["kit"] == str(state / "kit") and served["release_kit"] == str(root / "kit")
    assert "release_kit" not in bootstrap_result(root / "kit")
