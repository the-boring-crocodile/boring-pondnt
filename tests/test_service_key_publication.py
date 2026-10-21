"""Fail before client publication, retry, and retire only superseded setup keys."""
import asyncio
import time
from boring_pondnt.setup_services import plan, finalize, LABEL
from boring_service_kit import keys
from boring_service_kit.http import ServiceConfig


def entries(result):
    return {item["name"]: item["entry"] for item in result["services"]}


def prepare(servers, path, now):
    return entries(plan(servers, "C:/fixture", str(path), "", ["knowledge"],
                        "audit", "fixture", apply=True, now=now))


def test_interrupted_rotation_keeps_saved_key_and_retry_converges(tmp_path):
    now = time.time()
    saved = prepare({}, tmp_path, now - 340 * keys.DAY)
    path = tmp_path / "services/knowledge/keys.json"
    admin, _ = keys.add(path, "other", "reader", 1, "admin", now=now)
    interrupted = prepare(saved, tmp_path, now)
    verifier = ServiceConfig(path.parent / "config.json").keys
    old = saved["boring-knowledge"]["headers"]["Authorization"][7:]
    assert asyncio.run(verifier.verify_token(old)) is not None
    assert interrupted != saved
    retry = prepare(saved, tmp_path, now)
    result = finalize(retry, str(tmp_path), "audit", "fixture")
    assert result["removed"] == 2
    loaded = keys.KeyFile.load(path)
    assert admin in loaded.keys
    assert sum(k.label == LABEL for k in loaded.keys) == 1
    assert asyncio.run(verifier.verify_token(old)) is None
    assert prepare(retry, tmp_path, now) == retry


def test_finalize_invalid_published_entry_preserves_every_key(tmp_path):
    now = time.time()
    saved = prepare({}, tmp_path, now)
    path = tmp_path / "services/knowledge/keys.json"
    original = path.read_bytes()
    saved["boring-knowledge"]["headers"]["Authorization"] = "Bearer bsk_invalid"
    import pytest
    with pytest.raises(ValueError):
        finalize(saved, str(tmp_path), "audit", "fixture")
    assert path.read_bytes() == original
