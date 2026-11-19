"""Task registration contract on fake Scheduler APIs, also runnable on Linux."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not on PATH")
def test_logon_task_elevation_and_direct_python():
    root = Path(__file__).resolve().parents[1]
    setup = root / "tools/setup.ps1"
    if not setup.exists():
        setup = root / "setup.ps1"
    result = subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-File",
                             str(Path(__file__).with_name("setup_logon_fixture.ps1")), "-Setup", str(setup)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "synthetic APIs only" in result.stdout
