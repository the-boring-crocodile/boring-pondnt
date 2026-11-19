"""Publication races, cooperating writers and unsupported links use one disk owner."""
import concurrent.futures
import os
import multiprocessing
from pathlib import Path
import pytest
from boring_pondnt import disk, textfiles, binfiles
from boring_service_kit.outcomes import Refused


def test_competing_create_is_never_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "race.txt"
    original = disk.os.link
    def competitor(source, target, *args, **kwargs):
        Path(target).write_bytes(b"other writer")
        return original(source, target, *args, **kwargs)
    monkeypatch.setattr(disk.os, "link", competitor)
    with pytest.raises(Refused) as caught:
        textfiles.write_text(str(path), "ours", None)
    assert caught.value.code == "stale"
    assert path.read_bytes() == b"other writer"
    assert list(tmp_path.iterdir()) == [path]


def _write_once(path, sha, content):
    try:
        textfiles.write_text(path, content, sha)
        return "written"
    except Refused as refusal:
        return refusal.code


def test_cooperating_processes_compare_hash_under_one_lock(tmp_path):
    path = tmp_path / "shared.txt"
    path.write_bytes(b"original")
    sha = disk.sha256_bytes(path.read_bytes())
    context = multiprocessing.get_context("spawn")
    with concurrent.futures.ProcessPoolExecutor(max_workers=2, mp_context=context) as pool:
        jobs = [pool.submit(_write_once, str(path), sha, content) for content in ("one", "two")]
        assert sorted(job.result(timeout=30) for job in jobs) == ["stale", "written"]


@pytest.mark.parametrize("operation", ["write", "patch", "bytes"])
@pytest.mark.parametrize("kind", ["directory", "symlink", "fifo", "socket", "hardlink"])
def test_mutation_tools_report_the_forbidden_kind_without_changes(tmp_path, operation, kind):
    import socket
    target = tmp_path / "target"
    target.write_bytes(b"original")
    path = tmp_path / "named"
    server = None
    if kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        try:
            path.symlink_to(target)
        except OSError:
            pytest.skip("native symlink privilege unavailable")
    elif kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO fixture needs POSIX")
        os.mkfifo(path)
    elif kind == "socket":
        if os.name == "nt":
            pytest.skip("POSIX socket path fixture")
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(path))
    else:
        os.link(target, path)
    calls = {
        "write": lambda: textfiles.write_text(str(path), "new", None),
        "patch": lambda: textfiles.patch_text(str(path), disk.sha256_bytes(b"original"),
                                            [{"old_text": "original", "new_text": "new"}]),
        "bytes": lambda: binfiles.write_bytes(str(path), "eA==", None),
    }
    try:
        with pytest.raises(Refused) as caught:
            calls[operation]()
        data = caught.value.as_data()
        expected = kind if kind in ("directory", "symlink") else "other"
        assert (data["status"], data["code"], data["kind"]) == ("refused", "forbidden", expected)
        hint = {"directory": "file_list", "symlink": "do not follow links",
                "fifo": "FIFOs", "socket": "sockets", "hardlink": "multiple hard links"}[kind]
        assert hint in data["message"]
        assert target.read_bytes() == b"original"
    finally:
        if server is not None:
            server.close()


def test_hardlink_refusal_preserves_both_names(tmp_path):
    path = tmp_path / "original"
    path.write_bytes(b"original")
    other = tmp_path / "alias"
    os.link(path, other)
    with pytest.raises(Refused):
        binfiles.write_bytes(str(path), "eA==", disk.sha256_bytes(path.read_bytes()))
    assert path.read_bytes() == other.read_bytes() == b"original"


def test_symlink_refusal_preserves_link_and_referent(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"original")
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("native symlink privilege unavailable")
    with pytest.raises(Refused):
        textfiles.write_text(str(link), "ours", disk.sha256_bytes(target.read_bytes()))
    assert link.is_symlink() and target.read_bytes() == b"original"
