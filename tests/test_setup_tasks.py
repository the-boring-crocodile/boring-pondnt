"""Native PowerShell parser and ownership boundary; no real scheduler/process calls."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows PowerShell required")
@pytest.mark.parametrize("fixture", ["setup_tasks_fixture.ps1", "setup_client_config_fixture.ps1",
                                     "setup_lifecycle_fixture.ps1"])
def test_setup_task_ownership_without_host_mutations(fixture):
    root = Path(__file__).resolve().parents[1]
    setup = root / "setup.ps1"
    if not setup.exists():
        setup = root / "tools/setup.ps1"
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(Path(__file__).with_name(fixture)),
         "-Setup", str(setup), *([ "-Python", sys.executable ] if fixture == "setup_lifecycle_fixture.ps1" else [])],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "synthetic APIs only" in result.stdout


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows release required")
def test_installer_preview_preserves_fixture(tmp_path):
    root = Path(__file__).resolve().parents[1]
    setup = root / "setup.ps1"
    if not setup.exists():
        pytest.skip("full preview requires packaged runtime")
    (tmp_path / "source").mkdir()
    (tmp_path / "fixture.db").write_bytes(b"synthetic index, never opened")
    (tmp_path / "mcp.json").write_text('{"servers":{"unrelated":{"type":"http","url":"https://fixture.invalid"}}}')
    def snapshot():
        return {str(p.relative_to(tmp_path)): p.read_bytes()
                for p in tmp_path.rglob("*") if p.is_file()}
    before = snapshot()
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(Path(__file__).with_name("setup_preview_fixture.ps1")),
         "-Setup", str(setup), "-Fixture", str(tmp_path)],
        capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "readiness is unverified" in result.stdout
    assert "PASS: setup preview" in result.stdout
    assert snapshot() == before
    assert not (tmp_path / "local").exists()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 (pwsh) not on PATH")
def test_setup_functions_under_pwsh_without_the_task_scheduler():
    """The same synthetic checks on any OS with pwsh, without the Windows-only task cmdlets.
    Caught once: -Uninstall passed --plan and --uninstall to the planner."""
    root = Path(__file__).resolve().parents[1]
    setup = root / "setup.ps1"
    if not setup.exists():
        setup = root / "tools/setup.ps1"
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(Path(__file__).with_name("setup_tasks_fixture.ps1")),
         "-Setup", str(setup), "-NoNative"], capture_output=True, text=True, timeout=60,
        env={**__import__("os").environ, "USERDOMAIN": "FIXTURE", "USERNAME": "person"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "synthetic APIs only" in result.stdout
