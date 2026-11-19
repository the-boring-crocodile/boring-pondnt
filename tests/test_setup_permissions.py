"""Release ACL/owner control flow with fake Windows APIs on any PowerShell host."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_release_permission_inspection_warns_without_writing():
    shell = os.environ.get("BORING_TEST_POWERSHELL") or shutil.which("pwsh") or shutil.which("powershell.exe")
    if not shell:
        pytest.skip("PowerShell required for synthetic ACL fixture")
    root = Path(__file__).resolve().parents[1]
    setup = root / "setup.ps1"
    if not setup.exists():
        setup = root / "tools/setup.ps1"
    result = subprocess.run([
        shell, "-NoProfile", "-NonInteractive", "-File",
        str(Path(__file__).with_name("setup_permissions_fixture.ps1")),
        "-Setup", str(setup)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "synthetic APIs only" in result.stdout
