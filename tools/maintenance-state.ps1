# Shared record I/O for the optional administrative helpers. No effects when loaded.
Set-StrictMode -Version 2

function Assert-MaintenanceFile([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Record path must be an ordinary file: $Path"
        }
    }
}

function Open-MaintenanceLock([string]$Path) {
    $parent = Split-Path -Parent ([IO.Path]::GetFullPath($Path))
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
        throw "Choose an existing operator-owned record directory: $parent"
    }
    if ((Get-Item -LiteralPath $parent -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'Record directory must not be a reparse point.'
    }
    Assert-MaintenanceFile $Path
    Assert-MaintenanceFile ($Path + '.lock')
    return [IO.File]::Open($Path + '.lock', [IO.FileMode]::OpenOrCreate,
                          [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
}

function Read-MaintenanceRecord([string]$Path, [string]$Owner) {
    Assert-MaintenanceFile $Path
    if (-not (Test-Path -LiteralPath $Path)) {
        return [pscustomobject]@{schema=1; owner=$Owner; machine=$env:COMPUTERNAME; entries=@()}
    }
    if ((Get-Item -LiteralPath $Path).Length -gt 1MB) { throw 'Record exceeds 1 MiB.' }
    $data = [IO.File]::ReadAllText($Path, (New-Object Text.UTF8Encoding($false, $true))) | ConvertFrom-Json
    if (-not $data -or @($data.PSObject.Properties.Name).Count -ne 4 -or
        ($data.schema -isnot [int] -and $data.schema -isnot [long]) -or $data.schema -ne 1 -or
        $data.owner -isnot [string] -or $data.owner -cne $Owner -or
        $data.machine -isnot [string] -or $data.machine -cne $env:COMPUTERNAME -or
        $null -eq $data.entries -or $data.entries -isnot [array]) {
        throw 'Unsupported or foreign record; preserve it and review it by hand.'
    }
    return $data
}

function Write-MaintenanceRecord([string]$Path, $Record) {
    Assert-MaintenanceFile $Path
    $json = $Record | ConvertTo-Json -Depth 12
    $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($json + [Environment]::NewLine)
    if ($bytes.Length -gt 1MB) { throw 'Record exceeds 1 MiB.' }
    $temp = $Path + '.new-' + [guid]::NewGuid().ToString('N')
    try {
        $file = [IO.File]::Open($temp, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $file.Write($bytes, 0, $bytes.Length); $file.Flush($true) } finally { $file.Dispose() }
        if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temp, $Path, [NullString]::Value) }
        else { [IO.File]::Move($temp, $Path) }
    } finally {
        if (Test-Path -LiteralPath $temp) { [IO.File]::Delete($temp) }
    }
}
