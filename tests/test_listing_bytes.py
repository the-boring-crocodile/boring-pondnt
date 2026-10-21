"""Listing, literal search and raw bytes."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from boring_pondnt import binfiles, listing
from boring_pondnt.disk import sha256_bytes
from boring_service_kit.outcomes import Refused


def tree(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "AxClass").mkdir()
    (tmp_path / "src" / "AxClass" / "CustTable.xml").write_bytes(
        b"\xef\xbb\xbf<AxClass>\r\n  <Name>CustTable</Name>\r\n  <Label>@SYS12345</Label>\r\n</AxClass>\r\n"
    )
    (tmp_path / "src" / "labels.txt").write_bytes(b"\xff\xfe" + "@SYS12345=Kunde\r\n".encode("utf-16-le"))
    (tmp_path / "src" / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00@SYS12345")
    (tmp_path / "src" / ".git").mkdir()
    (tmp_path / "src" / ".git" / "config").write_text("@SYS12345")
    (tmp_path / "readme.md").write_text("nothing here\n")
    return tmp_path


def test_list_sorts_case_insensitively_and_types(tmp_path):
    root = tree(tmp_path)
    (root / "Zeta.txt").write_text("z")
    (root / "alpha.txt").write_text("a")
    result = listing.list_dir(str(root))
    assert [e["name"] for e in result["entries"]] == ["alpha.txt", "readme.md", "src", "Zeta.txt"]
    types = {e["name"]: e["type"] for e in result["entries"]}
    assert types["src"] == "dir" and types["alpha.txt"] == "file"
    assert result["total"] == 4 and not result["truncated"]


def test_list_pattern_and_limit(tmp_path):
    root = tree(tmp_path)
    for i in range(5):
        (root / f"f{i}.TXT").write_text("x")
    result = listing.list_dir(str(root), pattern="*.txt", limit=3)
    assert len(result["entries"]) == 3 and result["total"] == 5 and result["truncated"]


def test_list_refusals(tmp_path):
    with pytest.raises(Refused) as info:
        listing.list_dir(str(tmp_path / "missing"))
    assert info.value.code == "not_found"
    (tmp_path / "f").write_text("x")
    with pytest.raises(Refused) as info:
        listing.list_dir(str(tmp_path / "f"))
    assert info.value.code == "invalid_request"


def test_search_finds_text_across_encodings_and_skips_binary_and_git(tmp_path):
    root = tree(tmp_path)
    result = listing.search(str(root), "@SYS12345")
    found = {(Path(m["path"]).name, m["line"], m["text"]) for m in result["matches"]}
    assert found == {("CustTable.xml", 3, "<Label>@SYS12345</Label>"), ("labels.txt", 1, "@SYS12345=Kunde")}
    assert result["files_skipped"] == 1  # the png
    assert not result["truncated"]


def test_search_glob_case_and_single_file(tmp_path):
    root = tree(tmp_path)
    result = listing.search(str(root), "custtable", glob="*.xml", ignore_case=True)
    assert len(result["matches"]) == 1
    single = listing.search(str(root / "readme.md"), "nothing")
    assert single["matches"][0]["line"] == 1


def test_search_limit_marks_truncation(tmp_path):
    (tmp_path / "many.txt").write_text("hit\n" * 10)
    result = listing.search(str(tmp_path), "hit", limit=3)
    assert len(result["matches"]) == 3 and result["truncated"] and result["stopped"] == "limit"


def test_search_refusals(tmp_path):
    for query in ("", "a\nb"):
        with pytest.raises(Refused):
            listing.search(str(tmp_path), query)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_bytes_round_trip_and_ranges(tmp_path):
    path = tmp_path / "blob.bin"
    data = bytes(range(256)) * 4
    created = binfiles.write_bytes(str(path), b64(data), None)
    assert created["sha256"] == sha256_bytes(data) and path.read_bytes() == data
    part = binfiles.read_bytes(str(path), offset=1000, length=100)
    assert base64.b64decode(part["data"]) == data[1000:]
    assert part["length"] == 24 and part["eof"] and part["size"] == 1024


def test_bytes_ranged_write_splices_and_may_extend(tmp_path):
    path = tmp_path / "blob.bin"
    path.write_bytes(b"0123456789")
    sha = sha256_bytes(b"0123456789")
    result = binfiles.write_bytes(str(path), b64(b"AB"), sha, offset=3)
    assert path.read_bytes() == b"012AB56789"
    binfiles.write_bytes(str(path), b64(b"XYZ"), result["sha256"], offset=9)
    assert path.read_bytes() == b"012AB5678XYZ"


def test_bytes_refusals(tmp_path):
    path = tmp_path / "blob.bin"
    path.write_bytes(b"abc")
    sha = sha256_bytes(b"abc")
    cases = [
        (lambda: binfiles.write_bytes(str(path), b64(b"x"), None), "stale"),
        (lambda: binfiles.write_bytes(str(path), b64(b"x"), "0" * 64), "stale"),
        (lambda: binfiles.write_bytes(str(path), "not base64!", sha), "invalid_request"),
        (lambda: binfiles.write_bytes(str(path), b64(b"x"), sha, offset=10), "invalid_request"),
        (lambda: binfiles.write_bytes(str(path), b64(b"x"), sha, offset=-1), "invalid_request"),
        (lambda: binfiles.read_bytes(str(path), length=0), "invalid_request"),
        (lambda: binfiles.read_bytes(str(tmp_path / "missing")), "not_found"),
    ]
    for call, code in cases:
        with pytest.raises(Refused) as info:
            call()
        assert info.value.code == code
    assert path.read_bytes() == b"abc"


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="chmod semantics; Windows covered by the attribute")
def test_read_only_targets_are_refused_before_any_effect(tmp_path):
    from boring_pondnt import textfiles

    path = tmp_path / "Checked-in.xml"
    path.write_bytes(b"\xef\xbb\xbf<a/>\r\n")
    path.chmod(0o444)
    sha = textfiles.read_text(str(path))["sha256"]
    for call in (
        lambda: textfiles.patch_text(str(path), sha, [{"old_text": "<a/>", "new_text": "<b/>"}]),
        lambda: textfiles.write_text(str(path), "<b/>\n", sha),
        lambda: binfiles.write_bytes(str(path), b64(b"x"), sha),
    ):
        with pytest.raises(Refused) as info:
            call()
        assert info.value.code == "forbidden" and "check it out" in info.value.message
    assert path.read_bytes() == b"\xef\xbb\xbf<a/>\r\n"
    path.chmod(0o644)
