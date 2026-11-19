param([Parameter(Mandatory=$true)][string]$Setup)
$ErrorActionPreference='Stop'
$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Setup,[ref]$null,[ref]$errors)
if($errors.Count){throw ($errors | Out-String)}
foreach($name in @('Inspection-Path','Release-OpenWrites','Warn-ReleasePermissions','Assert-ExistingIndex')){
 $found=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$false))
 if($found.Count -ne 1){throw "Missing function $name"}
 . ([scriptblock]::Create($found[0].Extent.Text))
}
$root='C:\Program Files\pondNT\'+('a'*150)+'\'+('b'*150)
if($root.Length -le 260){throw 'Long-path fixture is too short'}
$trustedSids=@('S-1-5-32-544','S-1-5-18')
$writeMask=[long](0x2 -bor 0x4 -bor 0x10 -bor 0x100 -bor 0x10000 -bor 0x40 -bor 0x40000 -bor 0x80000 -bor 0x10000000 -bor 0x40000000)
$script:owner=$trustedSids[0];$script:rules=@();$script:seen=@()
function Say {param($Text,$Color) $script:said+=@($Text)}
function Get-Item {
 [CmdletBinding()]param($LiteralPath,[switch]$Force)
 if($script:enumerationDenied){throw 'synthetic enumeration failure'}
 $script:seen+=@($LiteralPath)
 [pscustomobject]@{FullName=$LiteralPath;Attributes=0}
}
function Get-ChildItem {[CmdletBinding()]param($LiteralPath,[switch]$Recurse,[switch]$Force) @()}
function Get-Acl {
 [CmdletBinding()]param($LiteralPath)
 if($script:aclDenied){throw 'synthetic ACL failure'}
 $acl=[pscustomobject]@{}
 $acl | Add-Member ScriptMethod GetOwner {param($Type) [pscustomobject]@{Value=$script:owner}}
 $acl | Add-Member ScriptMethod GetAccessRules {param($a,$b,$c) $script:rules}
 $acl
}
function Set-Acl {throw 'Permission inspection must not write'}
function icacls.exe {throw 'Permission inspection must not invoke icacls'}
if(@(Release-OpenWrites).Count){throw 'Trusted read-only release warned'}
if($script:seen[0] -cne ('\\?\'+$root)){throw 'Long path was not extended'}
foreach($case in @('owner','write','acl-denied','enumeration-denied')){
 $script:owner=$trustedSids[0];$script:rules=@();$script:aclDenied=$false;$script:enumerationDenied=$false;$script:said=@()
 switch($case){
  'owner' {$script:owner='S-1-5-21-fixture'}
  'write' {$script:rules=@([pscustomobject]@{AccessControlType='Allow';IdentityReference=[pscustomobject]@{Value='S-1-5-32-545'};PropagationFlags=0;FileSystemRights=2})}
  'acl-denied' {$script:aclDenied=$true}
  'enumeration-denied' {$script:enumerationDenied=$true}
 }
 Warn-ReleasePermissions
 if(($script:said -join ' ') -notmatch 'WARNING:.*Setup continues without changing ACLs'){throw "Warning/continue missing: $case"}
 if($case -match 'denied' -and ($script:said -join ' ') -notmatch 'unchecked'){throw 'Uninspected tree declared safe'}
}
'PASS: warning-only release inspection including long and unreadable paths; synthetic APIs only'
