"""Line windows match the established response without whole-log allocations."""
import tracemalloc

import pytest
from boring_pondnt import execs
from boring_pondnt.jobs import Job, Jobs, OUTPUT_MAX_CHARS


@pytest.mark.parametrize("data,fallback", [
    (b"", None), (b"one", None), (b"one\n", None), (b"\n\n", None),
    ("ä\r\n猫\nlast".encode(), None), (b"a\n\x84\r\nlast", "cp437"),
    (b"a\n\xff\n", None), (b"a" * 65535 + "猫\nend".encode(), None),
], ids=["empty", "partial", "terminated", "blank-lines", "utf8-crlf",
        "oem-fallback", "invalid-utf8", "utf8-chunk-boundary"])
@pytest.mark.parametrize("start,count", [(None, 2), (1, 2), (3, 1), (99, 2)])
def test_window_matches_prior_contract(tmp_path, data, fallback, start, count):
    path = tmp_path / "output"
    path.write_bytes(data)
    text, encoding = execs.decode_output(data, fallback)
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    first = max(0, len(lines) - count) if start is None else min(start - 1, len(lines))
    selected = lines[first:first + count]
    raw = "".join(line + "\n" for line in selected)
    expected = {"content": raw[-17:] if start is None else raw[:17],
                "start_line": first + 1, "returned_lines": len(selected),
                "total_lines": len(lines), "content_cut": len(raw) > 17,
                **({"encoding": encoding} if encoding != "utf-8" else {})}
    assert execs.line_window(path, start, count, 17, fallback) == expected


@pytest.mark.parametrize("start", [None, 1])
def test_public_job_output_is_bounded_even_for_a_giant_line(tmp_path, start):
    manager = Jobs(tmp_path)
    directory = manager.root / "synthetic"
    directory.mkdir()
    with (directory / "stdout").open("wb") as handle:
        handle.write(b"HEAD")
        for _ in range(128):
            handle.write(b"x" * 65536)
        handle.write(b"TAIL")
    manager._jobs["synthetic"] = Job("synthetic", "synthetic", "sh", str(tmp_path),
                                     1, directory, 0, state="exited")
    manager._jobs["synthetic"].done.set()  # this fixture represents an already settled job
    tracemalloc.start()
    try:
        result = manager.output("synthetic", start_line=start)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        manager.shutdown()
    assert peak < 2 * 1024 * 1024
    assert len(result["content"]) == OUTPUT_MAX_CHARS
    assert result["content_cut"] and result["total_lines"] == 1
    assert result["returned_lines"] == 1
    assert result["content"].endswith("TAIL\n") if start is None else result["content"].startswith("HEAD")


def test_d365_tail_cannot_chase_appended_output(tmp_path, monkeypatch):
    from io import BytesIO
    from boring_pondnt import d365

    class GrowingLog(BytesIO):
        def read(self, size=-1):
            assert size == 256 * 1024, "read must stay inside its selected byte extent"
            return b"kept\r\n#< CLIXML\n<Objs hidden\nlast\n"

    monkeypatch.setattr(d365, "open", lambda *args: GrowingLog(b"x" * (512 * 1024)), raising=False)
    manager = d365.Jobs("synthetic-powershell", tmp_path)
    manager._jobs["synthetic"] = {"log": str(tmp_path / "synthetic")}
    assert manager.tail("synthetic", 2) == ["kept", "last"]
