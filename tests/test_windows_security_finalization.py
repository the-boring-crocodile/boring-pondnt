"""Replacement security settlement and retention, without native API doubles."""
from pathlib import Path
from types import SimpleNamespace
import os
import pytest
from boring_pondnt import disk, windows_files as native
from boring_service_kit.outcomes import Failed


@pytest.mark.parametrize("changed", [False, True])
def test_security_restores_only_a_changed_native_result(monkeypatch, changed):
    state = {"security": "original"}
    writes = []
    monkeypatch.setattr(native, "_security_descriptor", lambda path: state["security"])
    monkeypatch.setattr(native, "_security_text", lambda descriptor: descriptor)
    def publish(*args):
        state["security"] = "merged" if changed else "original"
    def restore(path, information, descriptor):
        writes.append((information, descriptor))
        state["security"] = descriptor
    monkeypatch.setattr(native, "_replace_native", publish)
    monkeypatch.setattr(native, "_set_security", restore)
    native.replace(Path("target"), Path("replacement"), Path("backup"))
    assert writes == ([(4, "original")] if changed else [])
    assert state["security"] == "original"


@pytest.mark.parametrize("failure", ["read", "write", "mismatch"])
def test_security_failure_after_publication_is_distinguished(monkeypatch, failure):
    state = {"published": False}
    def read(path):
        if state["published"] and failure == "read":
            raise OSError("read failed")
        return "merged" if state["published"] else "original"
    monkeypatch.setattr(native, "_security_descriptor", read)
    monkeypatch.setattr(native, "_security_text", lambda descriptor: descriptor)
    monkeypatch.setattr(native, "_replace_native", lambda *args: state.update(published=True))
    def restore(*args):
        if failure == "write":
            raise OSError("restore failed")
    monkeypatch.setattr(native, "_set_security", restore)
    with pytest.raises(native.SecurityFinalizationError):
        native.replace(Path("target"), Path("replacement"), Path("backup"))
    assert state["published"]


def test_disk_retains_original_after_security_finalization_failure(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.write_bytes(b"original")
    # Replace only disk's OS reference, keeping pathlib/tempfile on their real OS.
    monkeypatch.setattr(disk, "os", SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(native, "prepare", lambda *args: None)
    def publish_then_fail(source, replacement, backup):
        os.replace(source, backup)
        os.replace(replacement, source)
        raise native.SecurityFinalizationError("injected security failure")
    monkeypatch.setattr(native, "replace", publish_then_fail)
    with pytest.raises(Failed) as caught:
        disk.replace_atomically(target, b"replacement")
    assert caught.value.code == "publication_uncertain"
    assert target.read_bytes() == b"replacement"
    originals = list(tmp_path.glob(".pondnt-original-*/original"))
    assert len(originals) == 1
    assert originals[0].read_bytes() == b"original"
