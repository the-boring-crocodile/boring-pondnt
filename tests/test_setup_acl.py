"""Native ACL check for newly created synthetic service state only."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

from boring_pondnt.setup_services import plan


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows DACL required")
def test_service_files_keep_private_fixture_acl(tmp_path):
    # Windows Python creates this temporary directory privately (including
    # OWNER RIGHTS on some versions). Never alter an existing user's ACL.
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory:
        state = Path(directory)
        plan({}, str(tmp_path / "release"), str(state), "", ["knowledge"],
             "fixture", "fixture", apply=True)
        script = """
$ErrorActionPreference='Stop'
$current=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$trusted=@($current,'S-1-5-18','S-1-5-32-544')
$root=Get-Acl -LiteralPath $env:PONDNT_ACL_FIXTURE
if(-not $root.AreAccessRulesProtected){throw 'Fixture root is not private'}
$paths=@($env:PONDNT_ACL_FIXTURE)
$paths+=@(Get-ChildItem -LiteralPath $env:PONDNT_ACL_FIXTURE -Recurse | ForEach-Object { $_.FullName })
foreach($path in $paths){
  $acl=Get-Acl -LiteralPath $path
  $owner=$acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
  if($owner -notin $trusted){throw 'Unexpected fixture owner'}
  if(@($acl.Access).Count -eq 0){throw 'Empty fixture DACL'}
  foreach($rule in $acl.Access){
    $id=$rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value
    if($rule.AccessControlType -eq 'Allow' -and $id -notin ($trusted+@('S-1-3-4'))){
      throw 'Service state grants access outside private parent contract'
    }
  }
}
Write-Output 'PASS: synthetic service state stays private.'
"""
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            env={**os.environ, "PONDNT_ACL_FIXTURE": str(state)},
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "stays private" in result.stdout
