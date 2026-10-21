"""Text files keep their encoding, BOM and line endings, byte for byte."""

from __future__ import annotations

import pytest

from boring_service_kit.outcomes import Refused
from boring_pondnt.textfiles import (
    TextFormat,
    decode,
    encode,
    patch_text,
    read_text,
    sha256_bytes,
    write_text,
)

SAMPLES = {
    "utf8_lf": "first\nzweite Zeile äöü\nthird\n".encode("utf-8"),
    "utf8_crlf": "first\r\nzweite Zeile äöü\r\nthird\r\n".encode("utf-8"),
    "utf8_bom_crlf": b"\xef\xbb\xbf" + "<?xml version=\"1.0\"?>\r\n<AxClass>äöü</AxClass>\r\n".encode("utf-8"),
    "utf16le_bom_crlf": b"\xff\xfe" + "LabelId=Text äöü\r\nOther=Zwei\r\n".encode("utf-16-le"),
    "utf16be_bom_lf": b"\xfe\xff" + "one\ntwo\n".encode("utf-16-be"),
    "no_final_newline": "a\r\nb".encode("utf-8"),
}


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_decode_encode_round_trips_bytes(name):
    data = SAMPLES[name]
    decoded = decode(data)
    assert "\r" not in decoded.text
    assert encode(decoded.text, decoded.format) == data


@pytest.mark.parametrize(
    "name, expected",
    [
        ("utf8_lf", TextFormat("utf-8", False, "lf")),
        ("utf8_crlf", TextFormat("utf-8", False, "crlf")),
        ("utf8_bom_crlf", TextFormat("utf-8", True, "crlf")),
        ("utf16le_bom_crlf", TextFormat("utf-16-le", True, "crlf")),
        ("utf16be_bom_lf", TextFormat("utf-16-be", True, "lf")),
    ],
)
def test_detects_format(name, expected):
    assert decode(SAMPLES[name]).format == expected


def test_mixed_newlines_detected():
    assert decode(b"a\r\nb\nc\r\n").format.newline == "mixed"
    assert decode(b"a\rb").format.newline == "mixed"
    assert decode(b"single line").format.newline == "none"


@pytest.mark.parametrize(
    "data",
    [b"\x00\x01\x02binary", b"\xff\xd8\xff\xe0 jpeg-ish \x80\x81", b"\xff\xfe\x00\x00utf32", "latin1 \xe4".encode("latin-1")],
)
def test_non_text_is_refused_as_binary(data):
    with pytest.raises(Refused) as info:
        decode(data)
    assert info.value.code == "invalid_request"


def put(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def test_read_shows_lf_text_and_format(tmp_path):
    path = put(tmp_path, "c.xml", SAMPLES["utf8_bom_crlf"])
    result = read_text(str(path))
    assert result["content"] == "<?xml version=\"1.0\"?>\n<AxClass>äöü</AxClass>\n"
    assert (result["encoding"], result["bom"], result["newline"]) == ("utf-8", True, "crlf")
    assert result["sha256"] == sha256_bytes(SAMPLES["utf8_bom_crlf"])
    assert result["total_lines"] == 2 and result["next_line"] is None and not result["truncated"]


def test_read_ranges_and_next_line(tmp_path):
    path = put(tmp_path, "n.txt", "".join(f"line {i}\r\n" for i in range(1, 11)).encode())
    first = read_text(str(path), start_line=1, line_count=4)
    assert first["content"] == "line 1\nline 2\nline 3\nline 4\n"
    assert first["next_line"] == 5 and first["truncated"]
    last = read_text(str(path), start_line=9, line_count=4)
    assert last["content"] == "line 9\nline 10\n"
    assert last["next_line"] is None and not last["truncated"]


def test_read_cuts_one_huge_line(tmp_path):
    path = put(tmp_path, "min.xml", b"x" * 100_000 + b"\n")
    result = read_text(str(path))
    assert len(result["content"]) == 64 * 1024 + 1
    assert result["line_cut"] and result["truncated"]


def test_patch_preserves_bom_and_crlf(tmp_path):
    path = put(tmp_path, "c.xml", SAMPLES["utf8_bom_crlf"])
    before = read_text(str(path))
    result = patch_text(
        str(path),
        before["sha256"],
        [{"old_text": "<AxClass>äöü</AxClass>\n", "new_text": "<AxClass>\n  <Name>Neu</Name>\n</AxClass>\n"}],
    )
    expected = b"\xef\xbb\xbf" + '<?xml version="1.0"?>\r\n<AxClass>\r\n  <Name>Neu</Name>\r\n</AxClass>\r\n'.encode()
    assert path.read_bytes() == expected
    assert result["sha256"] == sha256_bytes(expected)


def test_patch_preserves_utf16(tmp_path):
    path = put(tmp_path, "labels.txt", SAMPLES["utf16le_bom_crlf"])
    sha = read_text(str(path))["sha256"]
    patch_text(str(path), sha, [{"old_text": "Other=Zwei", "new_text": "Other=Drei"}])
    assert path.read_bytes() == b"\xff\xfe" + "LabelId=Text äöü\r\nOther=Drei\r\n".encode("utf-16-le")


def test_patch_accepts_crlf_in_old_text(tmp_path):
    path = put(tmp_path, "a.txt", SAMPLES["utf8_crlf"])
    sha = read_text(str(path))["sha256"]
    patch_text(str(path), sha, [{"old_text": "first\r\nzweite", "new_text": "erste\r\nzweite"}])
    assert path.read_bytes().startswith(b"erste\r\nzweite")


@pytest.mark.parametrize(
    "edits, code",
    [
        ([{"old_text": "missing", "new_text": "x"}], "invalid_request"),
        ([{"old_text": "i", "new_text": "x"}], "invalid_request"),
        ([{"old_text": "", "new_text": "x"}], "invalid_request"),
        ([{"old_text": "first", "new_text": "1st"}, {"old_text": "first", "new_text": "x"}], "invalid_request"),
        ([{"old_text": "first\nzw", "new_text": "a"}, {"old_text": "zweite", "new_text": "b"}], "invalid_request"),
    ],
)
def test_patch_refusals_change_nothing(tmp_path, edits, code):
    path = put(tmp_path, "a.txt", SAMPLES["utf8_crlf"])
    sha = read_text(str(path))["sha256"]
    with pytest.raises(Refused) as info:
        patch_text(str(path), sha, edits)
    assert info.value.code == code
    assert path.read_bytes() == SAMPLES["utf8_crlf"]


def test_patch_refuses_overlapping_occurrences(tmp_path):
    path = put(tmp_path, "ambiguous.txt", b"aaa")
    sha = read_text(str(path))["sha256"]
    with pytest.raises(Refused) as info:
        patch_text(str(path), sha, [{"old_text": "aa", "new_text": "b"}])
    assert info.value.code == "invalid_request"
    assert path.read_bytes() == b"aaa"


def test_stale_hash_is_refused(tmp_path):
    path = put(tmp_path, "a.txt", SAMPLES["utf8_lf"])
    with pytest.raises(Refused) as info:
        patch_text(str(path), "0" * 64, [{"old_text": "third", "new_text": "3"}])
    assert info.value.code == "stale"
    assert info.value.details["current_sha256"] == sha256_bytes(SAMPLES["utf8_lf"])
    with pytest.raises(Refused) as info:
        write_text(str(path), "x", "0" * 64)
    assert info.value.code == "stale"


def test_mixed_newlines_need_an_explicit_choice(tmp_path):
    path = put(tmp_path, "m.txt", b"a\r\nb\nc\r\n")
    sha = read_text(str(path))["sha256"]
    with pytest.raises(Refused) as info:
        patch_text(str(path), sha, [{"old_text": "b", "new_text": "B"}])
    assert info.value.code == "invalid_request"
    patch_text(str(path), sha, [{"old_text": "b", "new_text": "B"}], newline="crlf")
    assert path.read_bytes() == b"a\r\nB\r\nc\r\n"


def test_write_creates_with_defaults_and_refuses_existing(tmp_path):
    path = tmp_path / "new.txt"
    result = write_text(str(path), "a\nb\n", None)
    assert path.read_bytes() == b"a\r\nb\r\n"
    assert (result["encoding"], result["bom"], result["newline"]) == ("utf-8", False, "crlf")
    with pytest.raises(Refused) as info:
        write_text(str(path), "c", None)
    assert info.value.code == "stale"
    assert info.value.details["current_sha256"] == sha256_bytes(b"a\r\nb\r\n")


def test_write_replaces_keeping_format(tmp_path):
    path = put(tmp_path, "c.xml", SAMPLES["utf8_bom_crlf"])
    sha = read_text(str(path))["sha256"]
    write_text(str(path), "<x/>\n", sha)
    assert path.read_bytes() == b"\xef\xbb\xbf<x/>\r\n"


def test_write_can_change_format_explicitly(tmp_path):
    path = put(tmp_path, "a.txt", SAMPLES["utf8_crlf"])
    sha = read_text(str(path))["sha256"]
    write_text(str(path), "x\n", sha, encoding="utf-16-le", newline="lf")
    assert path.read_bytes() == b"\xff\xfe" + "x\n".encode("utf-16-le")


def test_relative_paths_and_missing_dirs_are_refused(tmp_path):
    with pytest.raises(Refused) as info:
        read_text("relative.txt")
    assert info.value.code == "invalid_request"
    with pytest.raises(Refused) as info:
        write_text(str(tmp_path / "nope" / "a.txt"), "x", None)
    assert info.value.code == "not_found"


def test_failed_write_leaves_no_temp_file(tmp_path, monkeypatch):
    import os

    from boring_pondnt import disk
    from boring_service_kit.outcomes import Failed

    path = put(tmp_path, "a.txt", SAMPLES["utf8_lf"])
    sha = read_text(str(path))["sha256"]

    def broken_replace(*args):
        raise OSError(5, "simulated failure")

    monkeypatch.setattr(disk, "_overwrite", broken_replace)
    with pytest.raises(Failed) as info:
        write_text(str(path), "x\n", sha)
    assert info.value.code == "write_incomplete"
    assert sorted(os.listdir(tmp_path)) == ["a.txt"]
    assert path.read_bytes() == SAMPLES["utf8_lf"]


def test_patch_edits_match_the_original_in_any_order(tmp_path):
    path = put(tmp_path, "a.txt", b"alpha\r\nbeta\r\ngamma\r\n")
    sha = read_text(str(path))["sha256"]
    patch_text(str(path), sha, [{"old_text": "gamma", "new_text": "G"}, {"old_text": "alpha", "new_text": "beta"}])
    assert path.read_bytes() == b"beta\r\nbeta\r\nG\r\n"


def test_oversized_files_are_refused_with_limit(tmp_path, monkeypatch):
    from boring_pondnt import textfiles

    monkeypatch.setattr(textfiles, "MAX_FILE_BYTES", 10)
    path = put(tmp_path, "big.txt", b"0123456789abc")
    with pytest.raises(Refused) as info:
        read_text(str(path))
    assert info.value.code == "limit"
    with pytest.raises(Refused) as info:
        write_text(str(tmp_path / "new.txt"), "0123456789abc", None)
    assert info.value.code == "limit"
    assert not (tmp_path / "new.txt").exists()
