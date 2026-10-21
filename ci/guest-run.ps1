<#
.SYNOPSIS
    pondNT CI, inside a disposable Windows guest: unpack the release, check, test.

.DESCRIPTION
    Runs under Windows PowerShell 5.1 (all Windows Server Core ships).
    1. Records the machine (OS, PowerShell, LongPathsEnabled).
    2. Enables LongPathsEnabled if it is off -- this guest is disposable, and
       it is what the install script does on a real machine.
    3. Unpacks the release zip (built with --with-tests) into a fresh folder.
    4. Runs `boring-pondnt-core.cmd --self-check`, `run-tests.cmd` (the whole
       suite, Windows-only tests included; Sourcecache's tests prove
       FTS5 and trigram on this Python) and `run-services-tests.cmd` (the bundled
       service and integration tests; a release without it is an error).
    5. Writes one UTF-8 JSON result file and exits 0 only if all three passed.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File guest-run.ps1 -Zip C:\ci\boring-pondnt-0.1.0.dev0-win-x64-tests.zip
#>
param(
    [Parameter(Mandatory = $true)][string]$Zip,
    [string]$Work = 'C:\boring-pondnt-ci',
    [string]$Results = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if (-not $Results) { $Results = Join-Path $Work 'results.json' }
$utf8 = New-Object System.Text.UTF8Encoding $false

function Invoke-Captured([string]$File, [string[]]$Arguments, [string]$Name) {
    # Separate files for stdout and stderr; the exit code from the process itself.
    $out = Join-Path $Work "$Name.stdout.txt"
    $err = Join-Path $Work "$Name.stderr.txt"
    $started = Get-Date
    $proc = Start-Process -FilePath $File -ArgumentList $Arguments -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $out -RedirectStandardError $err
    $stdout = [IO.File]::ReadAllText($out, [Text.Encoding]::UTF8)
    $stderr = [IO.File]::ReadAllText($err, [Text.Encoding]::UTF8)
    return [ordered]@{
        exit_code = $proc.ExitCode
        seconds   = [math]::Round(((Get-Date) - $started).TotalSeconds, 1)
        stdout    = $stdout
        stderr    = $stderr
    }
}

function Get-Tail([string]$Text, [int]$Lines) {
    $all = @($Text -split "`r?`n" | Where-Object { $_.Trim() -ne '' })
    return ($all | Select-Object -Last $Lines) -join "`n"
}

function Invoke-Suite([string]$Folder, [string]$Script, [string]$Name) {
    # One pytest wrapper of the release; its own exit code decides, junit names the failures.
    $file = Join-Path $Folder $Script
    if (-not (Test-Path -LiteralPath $file)) { throw "the release has no $Script (build it with --with-tests)" }
    $junit = Join-Path $Work "$Name.junit.xml"
    $run = Invoke-Captured $file @('-p', 'no:cacheprovider', "--junitxml=$junit") $Name
    $failed = @()
    if (Test-Path $junit) {
        [xml]$xml = Get-Content -Raw -Encoding UTF8 $junit
        $failed = @($xml.SelectNodes('//testcase[failure or error]') | ForEach-Object { "$($_.classname)::$($_.name)" })
    }
    return [ordered]@{
        exit_code = $run.exit_code
        seconds   = $run.seconds
        summary   = (Get-Tail $run.stdout 1)
        failed    = $failed
        output    = (Get-Tail $run.stdout 60)
        stderr    = (Get-Tail $run.stderr 20)
    }
}

$result = [ordered]@{ started = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'); ok = $false }
try {
    if (Test-Path $Work) { Remove-Item -Recurse -Force $Work }
    New-Item -ItemType Directory -Path $Work | Out-Null

    $os = Get-CimInstance Win32_OperatingSystem
    $fs = 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem'
    $longPaths = (Get-ItemProperty $fs).LongPathsEnabled
    $result.machine = [ordered]@{
        os                     = "$($os.Caption) $($os.Version)"
        powershell             = $PSVersionTable.PSVersion.ToString()
        long_paths_was_enabled = ($longPaths -eq 1)
    }
    if ($longPaths -ne 1) {
        Set-ItemProperty -Path $fs -Name LongPathsEnabled -Value 1 -Type DWord
        $result.machine.long_paths_enabled_by_ci = $true
    }

    Expand-Archive -LiteralPath $Zip -DestinationPath (Join-Path $Work 'release') -Force
    $release = Get-ChildItem (Join-Path $Work 'release') -Directory | Select-Object -First 1
    if (-not $release) { throw 'the zip contains no release folder' }
    $result.release = [ordered]@{
        folder   = $release.FullName
        manifest = (Get-Content -Raw -Encoding UTF8 (Join-Path $release.FullName 'MANIFEST.json') | ConvertFrom-Json)
    }

    $check = Invoke-Captured (Join-Path $release.FullName 'boring-pondnt-core.cmd') `
        @('--self-check', '--state-dir', (Join-Path $Work 'state')) 'self-check'
    $report = $null
    try { $report = $check.stdout | ConvertFrom-Json } catch { }
    $result.self_check = [ordered]@{
        exit_code = $check.exit_code
        seconds   = $check.seconds
        report    = $report
        stderr    = (Get-Tail $check.stderr 20)
    }

    $result.tests = Invoke-Suite $release.FullName 'run-tests.cmd' 'tests'
    # Review gap 5: the bundled service tests are part of the verdict, not inferred from run-tests.cmd.
    $result.services_tests = Invoke-Suite $release.FullName 'run-services-tests.cmd' 'services-tests'

    $result.ok = ($check.exit_code -eq 0) -and ($result.tests.exit_code -eq 0) -and
        ($result.services_tests.exit_code -eq 0)
}
catch {
    $result.error = "$($_.Exception.Message) at $($_.InvocationInfo.PositionMessage)"
}
finally {
    $result.finished = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    New-Item -ItemType Directory -Force -Path (Split-Path $Results) | Out-Null
    [IO.File]::WriteAllText($Results, ($result | ConvertTo-Json -Depth 8), $utf8)
}
if ($result.ok) { exit 0 } else { exit 1 }
