"""Stable setup bearers and repair from private header files; synthetic credentials only."""
import asyncio
import json

import pytest
from boring_pondnt import setup_services as setup
from boring_service_kit import keys
from boring_service_kit.http import ServiceConfig

NOW = 1_800_000_000.0


def prepare(state, servers=None, apply=True, rotate=False, now=NOW):
    return setup.plan(servers or {}, "C:/fixture", str(state), "", ["knowledge"],
                      "audit", "fixture", apply=apply, now=now, rotate_keys=rotate)


def entry(result):
    return result["services"][0]["entry"]


def snapshot(state):
    return {p: p.read_bytes() for p in state.rglob("*") if p.is_file()}


def test_rerun_repairs_missing_client_without_rotation_or_any_state_writes(tmp_path):
    first = entry(prepare(tmp_path))
    before = snapshot(tmp_path)
    assert entry(prepare(tmp_path, now=NOW + 400 * keys.DAY)) == first
    assert snapshot(tmp_path) == before
    assert len(keys.KeyFile.load(tmp_path / "services/knowledge/keys.json").keys) == 1


@pytest.mark.parametrize("source", ["direct", "gateway"])
def test_migration_retains_bearer_and_extends_annual_metadata_once(tmp_path, source):
    folder = tmp_path / "services/knowledge"
    folder.mkdir(parents=True)
    accepted, token = keys.add(folder / "keys.json", "audit@fixture", "writer", 365,
                               setup.LABEL, issuer="key:fixture-knowledge", now=NOW)
    old = {"type": "http", "url": setup.SERVICES["knowledge"].url,
           "headers": {"Authorization": "Bearer " + token}}
    servers = {"boring-knowledge": old} if source == "direct" else {}
    if source == "gateway":
        legacy = tmp_path / "services/gateway/knowledge.headers.json"
        legacy.parent.mkdir()
        legacy.write_text(json.dumps(old["headers"]))
    repaired = entry(prepare(tmp_path, servers, now=NOW + 340 * keys.DAY))
    assert repaired == old
    stored = keys.KeyFile.load(folder / "keys.json").keys[0]
    assert stored.id == accepted.id and stored.created == accepted.created
    assert stored.expires == NOW + (340 + setup.LOCAL_DAYS) * keys.DAY
    before = snapshot(tmp_path)
    assert entry(prepare(tmp_path, now=NOW + 500 * keys.DAY)) == old
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("source", ["direct", "gateway"])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_annual_migration_requires_a_currently_valid_bearer(tmp_path, source, offset):
    folder = tmp_path / "services/knowledge"
    folder.mkdir(parents=True)
    accepted, token = keys.add(folder / "keys.json", "audit@fixture", "writer", 365,
                               setup.LABEL, issuer="key:fixture-knowledge", now=NOW)
    old = {"type": "http", "url": setup.SERVICES["knowledge"].url,
           "headers": {"Authorization": "Bearer " + token}}
    servers = {"boring-knowledge": old} if source == "direct" else {}
    if source == "gateway":
        legacy = tmp_path / "services/gateway/knowledge.headers.json"
        legacy.parent.mkdir()
        legacy.write_text(json.dumps(old["headers"]))
    now = accepted.expires + offset
    before = snapshot(tmp_path)
    if offset < 0:
        assert not prepare(tmp_path, servers, apply=False, now=now)["services"][0]["new_key"]
        assert snapshot(tmp_path) == before
        assert entry(prepare(tmp_path, servers, now=now)) == old
        migrated = keys.KeyFile.load(folder / "keys.json").keys[0]
        assert migrated.sha256 == accepted.sha256 and migrated.id == accepted.id
        assert migrated.expires == now + setup.LOCAL_DAYS * keys.DAY
        return
    for apply in (False, True):
        with pytest.raises(ValueError, match="setup key expired; use -RotateKeys"):
            prepare(tmp_path, servers, apply=apply, now=now)
        assert snapshot(tmp_path) == before
    preview = prepare(tmp_path, servers, apply=False, rotate=True, now=now)
    assert preview["services"][0]["new_key"] and snapshot(tmp_path) == before
    rotated = entry(prepare(tmp_path, servers, rotate=True, now=now))
    assert rotated != old
    stored = keys.KeyFile.load(folder / "keys.json").keys
    assert len(stored) == 1 and stored[0].sha256 != accepted.sha256
    assert stored[0].created == now
    assert stored[0].expires == now + setup.LOCAL_DAYS * keys.DAY


def test_operator_keys_reference_is_used_for_repair_and_rotation(tmp_path):
    first = entry(prepare(tmp_path))
    folder = tmp_path / "services/knowledge"
    config = folder / "config.json"
    value = json.loads(config.read_text())
    value["keys"]["file"] = "private-accepted.json"
    config.write_text(json.dumps(value))
    old = folder / "keys.json"
    old.rename(folder / "private-accepted.json")
    assert entry(prepare(tmp_path)) == first
    changed = entry(prepare(tmp_path, rotate=True))
    assert changed != first and not old.exists()
    accepted = keys.KeyFile.load(folder / "private-accepted.json")
    assert accepted.keys[0].sha256 == keys.fingerprint(changed["headers"]["Authorization"][7:])


def test_explicit_rotation_revokes_only_our_setup_grant(tmp_path):
    old = entry(prepare(tmp_path))
    path = tmp_path / "services/knowledge/keys.json"
    admin, _ = keys.add(path, "audit@fixture", "reader", 10, "manual", now=NOW)
    other, _ = keys.add(path, "other@fixture", "writer", 10, setup.LABEL, now=NOW)
    before = snapshot(tmp_path)
    preview = prepare(tmp_path, {"boring-knowledge": old}, apply=False, rotate=True)
    assert preview["services"][0]["new_key"] and snapshot(tmp_path) == before
    changed = entry(prepare(tmp_path, {"boring-knowledge": old}, rotate=True))
    assert changed != old
    loaded = keys.KeyFile.load(path)
    assert admin in loaded.keys and other in loaded.keys
    assert len(loaded.keys) == 3
    verifier = ServiceConfig(path.parent / "config.json").keys
    assert asyncio.run(verifier.verify_token(old["headers"]["Authorization"][7:])) is None
    assert asyncio.run(verifier.verify_token(changed["headers"]["Authorization"][7:])) is not None
    assert entry(prepare(tmp_path, {"boring-knowledge": old})) == changed


@pytest.mark.parametrize("rotation", [False, True])
def test_interruption_after_bearer_publication_repairs_without_minting_again(tmp_path, monkeypatch, rotation):
    if rotation:
        prepare(tmp_path)
    save = keys.KeyFile.save
    def fail(*args):
        raise OSError("synthetic hash publication failure")
    monkeypatch.setattr(keys.KeyFile, "save", fail)
    with pytest.raises(OSError, match="hash publication"):
        prepare(tmp_path, rotate=rotation)
    header = tmp_path / "services/knowledge/client.headers.json"
    saved = header.read_bytes()
    monkeypatch.setattr(keys.KeyFile, "save", save)
    current = entry(prepare(tmp_path))
    assert header.read_bytes() == saved
    assert current["headers"] == json.loads(saved)
    assert len(keys.KeyFile.load(header.with_name("keys.json")).keys) == 1


@pytest.mark.parametrize("damage", ["invalid-header", "invalid-keys", "missing-header", "wrong-legacy"])
def test_invalid_or_conflicting_credentials_refuse_before_other_changes(tmp_path, damage):
    current = entry(prepare(tmp_path))
    folder = tmp_path / "services/knowledge"
    if damage == "invalid-header":
        (folder / "client.headers.json").write_text("not json")
    elif damage == "invalid-keys":
        (folder / "keys.json").write_text("not json")
    else:
        (folder / "client.headers.json").unlink()
        if damage == "wrong-legacy":
            current["headers"]["Authorization"] = "Bearer bsk_synthetic-wrong"
    before = snapshot(tmp_path)
    servers = {"boring-knowledge": current} if damage == "wrong-legacy" else {}
    with pytest.raises(ValueError):
        prepare(tmp_path, servers)
    assert snapshot(tmp_path) == before
