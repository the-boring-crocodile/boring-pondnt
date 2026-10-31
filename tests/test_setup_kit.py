"""The kit lives in the state folder: seeded once, never overwritten, differences reported."""
import json
from pathlib import Path

from boring_pondnt.server import bootstrap_result
from boring_pondnt.setup_services import SERVICES, plan, uninstall


def release(tmp_path: Path, name: str, persona: str = "Fitzwilliam") -> Path:
    root = tmp_path / name
    (root / "handbook").mkdir(parents=True)
    (root / "handbook" / "bootstrap.md").write_text("# bootstrap\n", encoding="utf-8")
    (root / "handbook" / "persona.md").write_text(f"{persona}\n", encoding="utf-8")
    return root


def setup(root: Path, state: Path, apply: bool = True) -> dict:
    return plan({}, str(root), str(state), "", ["core"], "Robin", "DEVBOX", apply=apply)


def test_first_install_seeds_the_state_kit_and_the_core_serves_it(tmp_path):
    root, state = release(tmp_path, "rc20"), tmp_path / "state"
    preview = setup(root, state, apply=False)
    assert preview["handbook"]["state"] == "new" and not (state / "handbook").exists()  # a plan writes nothing
    result = setup(root, state)
    assert result["handbook"]["seeded"] and result["handbook"]["differs"] == []
    assert (state / "handbook" / "persona.md").read_text(encoding="utf-8") == "Fitzwilliam\n"
    core = result["services"][0]
    assert core["arguments"][core["arguments"].index("--handbook") + 1] == str(state / "handbook")
    assert not [p for p in state.iterdir() if p.name.startswith(".handbook-")]  # no staging left


def test_a_later_release_never_overwrites_the_kit_and_reports_the_differences(tmp_path):
    state = tmp_path / "state"
    setup(release(tmp_path, "rc20"), state)
    (state / "handbook" / "persona.md").write_text("Fitzwilliam, as the owner adapted him\n", encoding="utf-8")
    (state / "handbook" / "project.md").write_text("our notes\n", encoding="utf-8")
    newer = release(tmp_path, "rc21", persona="Fitzwilliam, revised upstream")
    result = setup(newer, state)
    assert result["handbook"]["state"] == "kept" and "seeded" not in result["handbook"]
    assert result["handbook"]["differs"] == ["persona.md", "project.md"]
    assert (state / "handbook" / "persona.md").read_text(encoding="utf-8") == "Fitzwilliam, as the owner adapted him\n"


def test_a_kit_folder_without_bootstrap_is_left_alone_and_reported(tmp_path):
    state = tmp_path / "state"
    (state / "handbook").mkdir(parents=True)
    result = setup(release(tmp_path, "rc20"), state)
    assert result["handbook"]["state"] == "no-bootstrap" and not list((state / "handbook").iterdir())
    assert any(c.startswith("handbook: ") for c in result["conflicts"])


def test_uninstall_keeps_the_kit_and_names_it(tmp_path):
    root, state = release(tmp_path, "rc20"), tmp_path / "state"
    servers = {s["name"]: s["entry"] for s in setup(root, state)["services"]}
    answer = uninstall(servers, str(state), str(root))
    assert answer["handbook"] == str(state / "handbook") and (state / "handbook" / "bootstrap.md").is_file()
    assert json.dumps(answer)


def test_bootstrap_names_the_release_kit_when_serving_another(tmp_path, monkeypatch):
    root, state = release(tmp_path, "rc20"), tmp_path / "state"
    setup(root, state)
    monkeypatch.setattr("boring_pondnt.server.default_kit", lambda: root / "handbook")
    served = bootstrap_result(state / "handbook")
    assert served["handbook"] == str(state / "handbook") and served["release_handbook"] == str(root / "handbook")
    assert "release_handbook" not in bootstrap_result(root / "handbook")


def test_an_old_kit_install_is_refused_before_any_write_and_left_as_it_is(tmp_path):
    """Before the handbook the state folder held `kit`; this release does not migrate it."""
    root, state = release(tmp_path, 'new'), tmp_path / 'state'
    legacy = state / 'kit'
    legacy.mkdir(parents=True)
    (legacy / 'bootstrap.md').write_bytes(b'# owner\r\n')
    for extra in ((), ('handbook',)):
        for name in extra:
            (state / name).mkdir(exist_ok=True)
        for apply in (False, True):
            result = setup(root, state, apply=apply)
            assert result['handbook']['state'] == 'old-kit' and result['handbook']['legacy'] == str(legacy)
            assert not result['services']
            assert any('-Uninstall' in c and 'move that folder aside' in c for c in result['conflicts'])
            assert (legacy / 'bootstrap.md').read_bytes() == b'# owner\r\n'
            assert 'kit' not in result
    assert sorted(p.name for p in state.iterdir()) == ['handbook', 'kit']


def test_bootstrap_has_handbook_names_only(tmp_path):
    result = bootstrap_result(release(tmp_path, 'new') / 'handbook')
    assert result['handbook'] and [Path(p).name for p in result['handbook_files']] == ['persona.md']
    assert not {'kit', 'kit_files', 'release_kit'} & set(result)
