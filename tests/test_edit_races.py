"""An external change between the hash check and publication is refused.

Another program (an editor, Visual Studio) does not take pondNT's edit lock.
The edit runs in a worker thread; it pauses after its hash check, the external
change happens on the main thread, then publication runs. The edit must answer
`stale` and the external content must survive.
"""

from __future__ import annotations

import base64
import os
import threading

import pytest

from boring_pondnt import binfiles, disk, textfiles
from boring_service_kit.outcomes import Refused

ORIGINAL = b"original contents\r\n"


def edit_in_place(path, data: bytes) -> None:
    with open(path, "r+b") as handle:  # same file object, as an editor saving in place
        handle.write(data)
        handle.truncate(len(data))


def replace_path(path, data: bytes) -> None:
    fresh = path.with_name(path.name + ".saved")
    fresh.write_bytes(data)
    os.replace(fresh, path)  # a new file under the old name, as a save-by-rename


OPERATIONS = {
    "write_text": lambda path, sha: textfiles.write_text(str(path), "mine\n", sha),
    "patch_text": lambda path, sha: textfiles.patch_text(
        str(path), sha, [{"old_text": "original", "new_text": "mine"}]),
    "write_bytes": lambda path, sha: binfiles.write_bytes(
        str(path), base64.b64encode(b"mine").decode(), sha),
    "splice_bytes": lambda path, sha: binfiles.write_bytes(
        str(path), base64.b64encode(b"MINE").decode(), sha, offset=0),
}
CHANGES = {
    "same_size_in_place": (edit_in_place, b"external edit!!!!\r\n"),
    "longer_in_place": (edit_in_place, b"an external edit that is longer\r\n"),
    "replaced_path": (replace_path, b"original contents\r\n"),  # same bytes, another file
}


def race(monkeypatch, path, operation, change) -> BaseException | None:
    """Run `operation`; between its hash check and its publication, apply `change`."""
    checked, changed = threading.Event(), threading.Event()
    original = disk.require_writable

    def pause_after_check(p):
        original(p)
        checked.set()
        assert changed.wait(5), "the test must apply the external change"

    monkeypatch.setattr(disk, "require_writable", pause_after_check)
    outcome: list[BaseException | None] = [None]

    def edit() -> None:
        try:
            operation(path, disk.sha256_bytes(ORIGINAL))
        except BaseException as exc:  # noqa: BLE001 -- handed to the main thread
            outcome[0] = exc

    worker = threading.Thread(target=edit)
    worker.start()
    try:
        assert checked.wait(5), "the edit did not reach publication"
        change()
    finally:
        changed.set()
        worker.join(10)
    assert not worker.is_alive()
    return outcome[0]


@pytest.mark.parametrize("change_name", sorted(CHANGES))
@pytest.mark.parametrize("operation_name", sorted(OPERATIONS))
def test_an_external_change_after_the_check_is_refused_and_survives(
        tmp_path, monkeypatch, operation_name, change_name):
    path = tmp_path / "shared.txt"
    path.write_bytes(ORIGINAL)
    how, external = CHANGES[change_name]
    error = race(monkeypatch, path, OPERATIONS[operation_name], lambda: how(path, external))
    assert isinstance(error, Refused) and error.code == "stale", error
    assert path.read_bytes() == external


@pytest.mark.parametrize("operation_name", sorted(OPERATIONS))
def test_without_a_change_the_edit_publishes_in_place(tmp_path, monkeypatch, operation_name):
    path = tmp_path / "shared.txt"
    path.write_bytes(ORIGINAL)
    before = path.stat()
    assert race(monkeypatch, path, OPERATIONS[operation_name], lambda: None) is None
    assert path.read_bytes() != ORIGINAL
    assert (path.stat().st_dev, path.stat().st_ino) == (before.st_dev, before.st_ino)
