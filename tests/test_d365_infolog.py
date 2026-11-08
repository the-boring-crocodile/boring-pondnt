"""Dev-box system-message samples and malformed INFO containers."""
import json
import struct
from pathlib import Path

import pytest

from boring_pondnt.d365_infolog import MAX_ENTRIES, MAX_INFO_BYTES, Message, decode_info

SAMPLES = {x["kind"]: bytes.fromhex(x["hex"]) for x in
           json.loads((Path(__file__).parent / "fixtures" / "info-samples.json").read_text(encoding="utf-8"))}
HEADER = b"\x07\xfd\x07\x07\xfd\x01\x01\0\0\0\xff"


def message(text, kind=3, tail=b""):
    return b"\x07\x07\xfd\x01" + struct.pack("<i", kind) + b"\0" + text.encode("utf-16-le") + b"\0\0\0\0\0" + tail + b"\xff"


@pytest.mark.parametrize("sample,expected", [
    ("error", Message(3, "Batch task failed: Unable to construct an object from the class '' "
     "(Class Number '50784' Job Id '52665621683' ) in the batch framework. Make sure that the X++ "
     "code has been compiled to Microsoft .NET Framework CIL, and that the constructor does not "
     "require any parameters.", "BatchTaskFailed")),
    ("finished_small", Message(0, "Scheduler tasks scheduled.  Job: ZZREG-IMPORT-2026-11-04T16:10:38-"
     "B0D317EAD4054537A763546E7883E398, Entity task: DMFStagingWriter, Tasks scheduled: 1",
     "SchedulerTasksScheduledMesg")),
    ("finished_big", Message(1, "For additional warnings or errors, please check the log", "")),
])
def test_infolog_samples_from_a_test_box(sample, expected):
    assert decode_info(SAMPLES[sample]) == ([expected], None)


@pytest.mark.parametrize("tail", [b"\xfe", b"\x01\0", b"\x00a", b"\xfb\xff\xff\xff\xff",
                                   b"\x07\x07\xfd" * 16 + b"\xff" * 16])
def test_bad_tail_keeps_message_and_counts_undecoded_bytes(tail):
    data = HEADER + message("earlier") + message("current", tail=tail) + b"\xff"
    messages, remaining = decode_info(data)
    assert messages == [Message(3, "earlier", ""), Message(3, "current", "")]
    assert remaining is not None and remaining > 0


def test_opaque_length_checked_in_live_sample():
    data = SAMPLES["finished_big"]
    assert data[213] == 0xfb and int.from_bytes(data[214:218], "little") == 2217
    assert data[218 + 2217:] == b"\xff" * 3
    shortened = data[:218] + data[218:-4]
    assert decode_info(shortened) == ([Message(1, "For additional warnings or errors, please check the log", "")],
                                       len(shortened) - 213)


def test_missing_terminator_and_bounds():
    assert decode_info(HEADER + message("kept")) == ([Message(3, "kept", "")], 0)
    assert decode_info(b"x" * (MAX_INFO_BYTES + 1)) == ([], MAX_INFO_BYTES + 1)
    record = message("entry")
    messages, remaining = decode_info(HEADER + record * (MAX_ENTRIES + 1) + b"\xff")
    assert len(messages) == MAX_ENTRIES and remaining == len(record) + 1
