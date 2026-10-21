"""Native NTFS acceptance; never changes existing user files or global ACLs."""
import ctypes
import os
from pathlib import Path
import subprocess
import pytest
from boring_pondnt import disk, textfiles

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows NTFS required")


def security(path, protect=False):
    script = """
$ErrorActionPreference='Stop'
$p=$env:PONDNT_NATIVE_FILE
if($env:PONDNT_NATIVE_PROTECT -eq 'yes'){
  $acl=Get-Acl -LiteralPath $p
  $acl.SetAccessRuleProtection($true,$true)
  Set-Acl -LiteralPath $p -AclObject $acl
}
(Get-Acl -LiteralPath $p).Sddl
"""
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            env={**os.environ, "PONDNT_NATIVE_FILE": str(path),
                                 "PONDNT_NATIVE_PROTECT": "yes" if protect else "no"},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize("protected", [False, True])
def test_native_write_preserves_security_stream_and_hidden_system_attributes(tmp_path, protected):
    path = tmp_path / "metadata.txt"
    path.write_bytes(b"original")
    stream = Path(str(path) + ":fixture")
    stream.write_bytes(b"stream preserved")
    before = security(path, protect=protected)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    set_attributes = kernel.SetFileAttributesW
    set_attributes.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    assert set_attributes(str(path), 0x2 | 0x4 | 0x20)
    textfiles.write_text(str(path), "replacement", disk.sha256_bytes(path.read_bytes()))
    assert path.read_bytes() == b"replacement"
    assert stream.read_bytes() == b"stream preserved"
    assert security(path) == before  # includes owner, group, DACL/control flags
    assert path.stat().st_file_attributes & 0x6 == 0x6
    assert not list(tmp_path.glob(".pondnt-original-*"))
    set_attributes(str(path), 0x20)


def test_native_named_stream_write_refuses_without_effect(tmp_path):
    path = tmp_path / "streamed"
    path.write_bytes(b"original")
    stream = Path(str(path) + ":fixture")
    stream.write_bytes(b"keep")
    from boring_service_kit.outcomes import Refused
    with pytest.raises(Refused):
        textfiles.write_text(str(stream), "replacement", disk.sha256_bytes(stream.read_bytes()))
    assert stream.read_bytes() == b"keep" and path.read_bytes() == b"original"
