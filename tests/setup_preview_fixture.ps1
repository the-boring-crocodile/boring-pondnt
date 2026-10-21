param([string]$Setup,[string]$Fixture)
$ErrorActionPreference='Stop'
# Child PowerShell with fixture-only state; replace all machine-change APIs.
function Get-ScheduledTask { return $null }
function Get-CimInstance { return @() }
function Get-PSDrive {
    [pscustomobject]@{Root=[IO.Path]::GetPathRoot($Fixture);Free=20GB}
}
function Stop-Process { throw 'Preview attempted process termination' }
function Stop-ScheduledTask { throw 'Preview attempted task stop' }
function Register-ScheduledTask { throw 'Preview attempted task registration' }
function Unregister-ScheduledTask { throw 'Preview attempted task removal' }
function Start-ScheduledTask { throw 'Preview attempted task start' }
function Add-MpPreference { throw 'Preview attempted Defender change' }
function Get-NetTCPConnection { throw 'Preview attempted listener readiness' }
function Read-Host { throw 'Preview attempted interactive confirmation' }
$env:LOCALAPPDATA=Join-Path $Fixture 'local'
$env:PYTHONDONTWRITEBYTECODE='1'
& $Setup -WhatIf -SkipIndex -Source (Join-Path $Fixture 'source') -Index (Join-Path $Fixture 'fixture.db') -McpJson (Join-Path $Fixture 'mcp.json') -Services 'knowledge,canvas'
Write-Output 'PASS: setup preview returned without machine mutations.'
