"""Special paths are refused by every file tool (Wald, 2026-11-07): shares, device paths, streams, device names."""
import pytest

from boring_pondnt import disk
from boring_service_kit.outcomes import Refused


@pytest.mark.parametrize("path", [
    r"\\host\share\file.txt", "//host/share/file.txt", r"\\?\GLOBALROOT\Device\X", r"\\.\pipe\name",
    r"\??\C:\x", r"C:\data\file.txt:stream", r"C:\data\CON", r"C:\data\nul.txt", r"C:\data\COM1\x",
    r"C:\data\LPT9 .txt"])
def test_special_paths_are_refused(path):
    with pytest.raises(Refused) as caught:
        disk.absolute(path)
    assert caught.value.message != "path must be absolute"  # refused as special, on every platform


@pytest.mark.parametrize("path", [r"C:\data\console.txt", r"C:\data\connect", r"C:\nullable\x.txt"])
def test_names_that_only_start_like_a_device_pass_the_special_check(path):
    try:
        disk.absolute(path)
    except Refused as error:  # off Windows a drive path is not absolute: that refusal is fine
        assert error.message == "path must be absolute"
