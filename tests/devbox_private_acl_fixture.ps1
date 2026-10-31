param([string]$Helper, [string]$Fixture)
$ErrorActionPreference = 'Stop'
. $Helper
$root = Join-Path $Fixture 'credentials'
$null = New-Item -ItemType Directory -Path $root
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = New-Object Security.AccessControl.DirectorySecurity
$acl.SetOwner($sid)
$acl.SetAccessRuleProtection($true, $false)
$inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
$rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', $inherit, 'None', 'Allow')
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $root -AclObject $acl
Set-Content -LiteralPath (Join-Path $root 'synthetic-secret') -Value 'fixture-only'
Assert-PrivateDevboxCredentials $root
$ownerRights = New-Object Security.Principal.SecurityIdentifier('S-1-3-4')
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($ownerRights, 'Modify', $inherit, 'None', 'Allow')))
Set-Acl -LiteralPath $root -AclObject $acl
Assert-PrivateDevboxCredentials $root
$everyone = New-Object Security.Principal.SecurityIdentifier('S-1-1-0')
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($everyone, 'ReadAndExecute', $inherit, 'None', 'Allow')))
Set-Acl -LiteralPath $root -AclObject $acl
$refused = $false
try { Assert-PrivateDevboxCredentials $root } catch { $refused = $true }
if (-not $refused) { throw 'Broad credential ACL was accepted' }
'PASS: private credential tree accepted (also with OWNER RIGHTS); broad read grant refused'
