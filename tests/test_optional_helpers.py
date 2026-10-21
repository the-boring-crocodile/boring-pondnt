"""Real record I/O plus synthetic process/Defender APIs; no machine mutations."""
import os
from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("fixture,helper", [
    ("defender_recovery_fixture.ps1", "devbox-defender.ps1"),
    ("devbox_ownership_fixture.ps1", "devbox-start.ps1"),
])
def test_optional_helper_contracts(tmp_path, fixture, helper):
    if sys.platform == "win32":
        powershell = "powershell.exe"
    else:
        powershell = os.environ.get("BORING_TEST_POWERSHELL")
        if not powershell:
            pytest.skip("native Windows PowerShell, or explicitly selected portable fixture runtime")
    source = ROOT / "tools" / helper
    if not source.exists():
        source = ROOT / helper if helper == "devbox-defender.ps1" else ROOT / "extras/pond-tunnel" / helper
    assert source.is_file(), "packaged helper source missing"
    assert (source.parent / "maintenance-state.ps1").is_file(), "shared record owner missing"
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(Path(__file__).with_name(fixture)), "-Helper", str(source), "-Fixture", str(tmp_path)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "synthetic APIs only" in result.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows CIM and process handles required")
def test_native_helper_process_identity(tmp_path):
    source = ROOT / "tools/devbox-start.ps1"
    if not source.exists():
        source = ROOT / "extras/pond-tunnel/devbox-start.ps1"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(Path(__file__).with_name("devbox_native_process_fixture.ps1")),
         "-Helper", str(source), "-Fixture", str(tmp_path)],
        capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "test child only" in result.stdout
