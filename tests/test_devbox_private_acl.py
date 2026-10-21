import os
from pathlib import Path
import subprocess
import pytest

@pytest.mark.skipif(os.name != "nt", reason="native Windows credential ACL fixture")
def test_private_devbox_credentials(tmp_path):
    root = Path(__file__).resolve().parent.parent
    helper = root / "tools" / "devbox-start.ps1"
    if not helper.exists():
        helper = root / "extras" / "pond-tunnel" / "devbox-start.ps1"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-File",
        str(Path(__file__).with_name("devbox_private_acl_fixture.ps1")),
        "-Helper", str(helper), "-Fixture", str(tmp_path)],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
