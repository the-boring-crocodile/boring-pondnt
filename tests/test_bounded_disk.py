"""A stale stat must not turn a bounded file operation into an unbounded read."""
from io import BytesIO
from types import SimpleNamespace

import pytest

from boring_pondnt import disk, listing
from boring_service_kit.outcomes import Refused


class GrowingFile:
    def stat(self):
        return SimpleNamespace(st_size=1)

    def is_dir(self):
        return False

    def open(self, mode):
        assert mode == "rb"
        class LimitedRead(BytesIO):
            def read(self, size=-1):
                assert size == 5, "must request only limit + 1 bytes"
                return super().read(size)
        return LimitedRead(b"abcdef")

    def read_bytes(self):
        return b"abcdef"

    def __str__(self):
        return "synthetic-growing-file"


def test_growth_after_stat_is_refused():
    with pytest.raises(Refused) as error:
        disk.read_bytes(GrowingFile(), 4)
    assert error.value.code == "limit"


def test_search_uses_bounded_disk_reader(tmp_path, monkeypatch):
    path = tmp_path / "growing.txt"
    path.write_text("hit")
    calls = []
    def growing(file, limit):
        calls.append((file, limit))
        raise Refused("limit", "file grew")
    monkeypatch.setattr(disk, "read_bytes", growing)
    result = listing.search(str(path), "hit")
    assert calls == [(path, listing.MAX_FILE_BYTES)]
    assert result["matches"] == []
    assert result["files_skipped"] == 1
