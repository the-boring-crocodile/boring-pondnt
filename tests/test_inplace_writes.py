"""Existing-file identity and explicit partial-write failure contract."""
import pytest
from boring_pondnt import disk, textfiles, binfiles
from boring_service_kit.outcomes import Failed, Refused


def test_shorter_edit_keeps_file_identity_and_truncates_tail(tmp_path):
    path = tmp_path / "original"
    path.write_bytes(b"long original contents")
    before = path.stat()
    textfiles.write_text(str(path), "new", disk.sha256_bytes(path.read_bytes()))
    after = path.stat()
    assert path.read_bytes() == b"new"
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)


def test_partial_write_is_reported_without_silent_rollback(tmp_path, monkeypatch):
    path = tmp_path / "original"
    path.write_bytes(b"original")
    def interrupted(handle, data):
        handle.write(data[:2])
        raise OSError("injected interruption")
    monkeypatch.setattr(disk, "_overwrite", interrupted)
    _, version = disk.read_version(path, 100)
    with pytest.raises(Failed) as caught:
        disk.write_contents(path, b"replacement", expected=version)
    assert caught.value.code == "write_incomplete"
    assert path.read_bytes() == b"reiginal"
    assert list(tmp_path.iterdir()) == [path]


def test_missing_original_is_not_recreated(tmp_path):
    path = tmp_path / "missing"
    with pytest.raises(Refused) as caught:
        disk.write_contents(path, b"replacement", expected=disk.Version(0, 0, 0, disk.sha256_bytes(b"")))
    assert caught.value.code == "not_found"
    assert not path.exists()


def test_inplace_writer_finishes_short_os_writes():
    class ShortWriter:
        def __init__(self):
            self.data = bytearray()
            self.size = None
        def write(self, data):
            self.data.extend(data[:2])
            return len(data[:2])
        def truncate(self, size):
            self.size = size
    handle = ShortWriter()
    disk._overwrite(handle, b"abcdef")
    assert handle.data == b"abcdef"
    assert handle.size == 6


def test_binary_splice_keeps_identity(tmp_path):
    path = tmp_path / "binary"
    path.write_bytes(b"abcdef")
    before = path.stat().st_ino
    binfiles.write_bytes(str(path), "WFk=", disk.sha256_bytes(path.read_bytes()), offset=2)
    assert path.read_bytes() == b"abXYef"
    assert path.stat().st_ino == before
