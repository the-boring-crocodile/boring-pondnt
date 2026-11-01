"""browser-signin.ps1: configuration written or reused, refusals; Edge is a placeholder, never started."""
from pathlib import Path
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell 5.1 runs the helper")
def test_browser_signin_cases(tmp_path):
    source = ROOT / "tools" / "browser-signin.ps1"
    if not source.exists():
        source = ROOT / "browser-signin.ps1"  # the release folder
    assert source.is_file(), "browser-signin.ps1 missing"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(Path(__file__).with_name("browser_signin_fixture.ps1")),
         "-Helper", str(source), "-Fixture", str(tmp_path)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: browser-signin cases" in result.stdout


def test_release_ships_the_signin_helper():
    if not (ROOT / "scripts" / "build.py").exists():
        assert (ROOT / "browser-signin.cmd").is_file() and (ROOT / "browser-signin.ps1").is_file()
        return
    build = (ROOT / "scripts" / "build.py").read_text(encoding="utf-8")
    assert '"browser-signin.ps1"' in build and "browser-signin.cmd" in build
