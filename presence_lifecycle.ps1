Set-StrictMode -Version 2.0

function Test-PresenceSamePath {
    [CmdletBinding()]
    param([AllowNull()][string]$Left, [AllowNull()][string]$Right)

    if (-not $Left -or -not $Right) {
        return $false
    }
    try {
        $trim = [char[]]@('\', '/')
        $leftFull = [IO.Path]::GetFullPath($Left).TrimEnd($trim)
        $rightFull = [IO.Path]::GetFullPath($Right).TrimEnd($trim)
        return [StringComparer]::OrdinalIgnoreCase.Equals($leftFull, $rightFull)
    } catch {
        return $false
    }
}

function Invoke-PresenceNativeCommand {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$ArgumentList = @()
    )

    $output = @(& $FilePath @ArgumentList 2>&1)
    $exitCode = $LASTEXITCODE
    if ($null -eq $exitCode) {
        throw "Could not determine the exit code from $FilePath."
    }
    if ($exitCode -ne 0) {
        $detail = if ($output.Count -gt 0) {
            ": " + (($output | ForEach-Object { "$_".Trim() }) -join [Environment]::NewLine)
        } else { "" }
        throw "$([IO.Path]::GetFileName($FilePath)) failed with exit code $exitCode$detail"
    }
    return $output
}

function Get-PresenceRuntimeConfig {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$PythonPath)

    $configCode = @"
import json
import sys
from pathlib import Path
from jarvis import __version__
from jarvis.config import Config, SOURCE_ROOT
from jarvis.presence_identity import normalized_install_path, presence_installation_id
config = Config.load()
pythonw = Path(sys.executable).with_name('pythonw.exe').resolve()
source_root = normalized_install_path(SOURCE_ROOT)
print('JARVIS_PRESENCE_CONFIG=' + json.dumps({
    'port': config.presence_port,
    'pythonw': str(pythonw),
    'source_root': source_root,
    'version': __version__,
    'installation_id': presence_installation_id(source_root=source_root, python_executable=pythonw),
    'state_file': str((config.data_dir / 'presence-manual.json').resolve()),
}, ensure_ascii=True))
"@
    $output = @(Invoke-PresenceNativeCommand -FilePath $PythonPath -ArgumentList @("-X", "utf8", "-c", $configCode))
    $prefix = "JARVIS_PRESENCE_CONFIG="
    $line = $output |
        ForEach-Object { "$_".Trim() } |
        Where-Object { $_.StartsWith($prefix, [StringComparison]::Ordinal) } |
        Select-Object -Last 1
    if (-not $line) {
        throw "Jarvis did not return its Presence runtime configuration."
    }
    try {
        $decoded = $line.Substring($prefix.Length) | ConvertFrom-Json -ErrorAction Stop
        $port = [int]$decoded.port
        if ($port -lt 1024 -or $port -gt 65535) {
            throw "Presence port was outside 1024..65535"
        }
        $pythonw = [IO.Path]::GetFullPath("$($decoded.pythonw)")
        if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
            throw "pythonw executable does not exist"
        }
        $sourceRoot = [IO.Path]::GetFullPath("$($decoded.source_root)")
        $stateFile = [IO.Path]::GetFullPath("$($decoded.state_file)")
        $version = "$($decoded.version)"
        $installationId = "$($decoded.installation_id)"
        if ($version -notmatch '^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$') {
            throw "Presence version was invalid"
        }
        if ($installationId -notmatch '^[0-9a-f]{64}$') {
            throw "Presence installation ID was invalid"
        }
        return [pscustomobject]@{
            Port = $port
            Pythonw = $pythonw
            SourceRoot = $sourceRoot
            Version = $version
            InstallationId = $installationId
            StateFile = $stateFile
            Url = "http://127.0.0.1:$port/"
            HealthUrl = "http://127.0.0.1:$port/api/health"
        }
    } catch {
        throw "Jarvis returned malformed Presence runtime configuration: $($_.Exception.Message)"
    }
}

function Get-PresenceHealth {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$HealthUrl)

    try {
        $response = Invoke-RestMethod -Uri $HealthUrl -Method Get -TimeoutSec 2 -ErrorAction Stop
        if ($response.service -ne "jarvis-presence" -or $response.ready -ne $true) {
            return $null
        }
        return $response
    } catch {
        return $null
    }
}

function Test-PresenceHealthIdentity {
    [CmdletBinding()]
    param(
        [AllowNull()]$Health,
        [Parameter(Mandatory = $true)]$Runtime
    )

    if ($null -eq $Health) {
        return $false
    }
    $processId = 0
    return (
        "$($Health.service)" -ceq "jarvis-presence" -and
        $Health.ready -eq $true -and
        "$($Health.version)" -ceq "$($Runtime.Version)" -and
        "$($Health.installation_id)" -ceq "$($Runtime.InstallationId)" -and
        (Test-PresenceSamePath -Left "$($Health.source_root)" -Right "$($Runtime.SourceRoot)") -and
        (Test-PresenceSamePath -Left "$($Health.python_executable)" -Right "$($Runtime.Pythonw)") -and
        "$($Health.runtime_epoch)" -match '^[0-9a-f]{32}$' -and
        [int]::TryParse("$($Health.process_id)", [ref]$processId) -and
        $processId -gt 0
    )
}

function Assert-PresenceHealthIdentity {
    [CmdletBinding()]
    param(
        [AllowNull()]$Health,
        [Parameter(Mandatory = $true)]$Runtime
    )

    if (-not (Test-PresenceHealthIdentity -Health $Health -Runtime $Runtime)) {
        throw "The Presence endpoint belongs to a different or stale Jarvis installation."
    }
}

function Wait-PresenceHealth {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Runtime,
        [AllowNull()][string]$PreviousRuntimeEpoch = $null,
        [int]$Attempts = 120
    )

    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        $health = Get-PresenceHealth -HealthUrl $Runtime.HealthUrl
        if (Test-PresenceHealthIdentity -Health $health -Runtime $Runtime) {
            if (-not $PreviousRuntimeEpoch -or "$($health.runtime_epoch)" -cne $PreviousRuntimeEpoch) {
                return $health
            }
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Jarvis Presence did not produce a fresh healthy endpoint with the expected installation identity within 30 seconds."
}

function Wait-PresenceOffline {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$HealthUrl, [int]$Attempts = 60)

    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        if ($null -eq (Get-PresenceHealth -HealthUrl $HealthUrl)) {
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "The previous Presence endpoint remained healthy after its exact process was stopped."
}

function Get-ExactPresenceProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Health,
        [Parameter(Mandatory = $true)]$Runtime
    )

    Assert-PresenceHealthIdentity -Health $Health -Runtime $Runtime
    $processId = [int]$Health.process_id
    try {
        $process = Get-Process -Id $processId -ErrorAction Stop
    } catch {
        throw "Presence reported process $processId, but that exact process no longer exists."
    }
    $processPath = $null
    try { $processPath = "$($process.Path)" } catch { $processPath = $null }
    if (-not (Test-PresenceSamePath -Left $processPath -Right $Runtime.Pythonw)) {
        throw "Refusing to stop process $processId because its executable does not match this Jarvis installation."
    }
    return $process
}

function Remove-PresenceManualState {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Runtime)

    if (Test-Path -LiteralPath $Runtime.StateFile -PathType Leaf) {
        Remove-Item -LiteralPath $Runtime.StateFile -Force -ErrorAction Stop
    }
}

function Get-PresenceManualState {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Runtime)

    if (-not (Test-Path -LiteralPath $Runtime.StateFile -PathType Leaf)) {
        return $null
    }
    try {
        $state = Get-Content -LiteralPath $Runtime.StateFile -Raw -Encoding UTF8 |
            ConvertFrom-Json -ErrorAction Stop
        $processId = 0
        if (
            "$($state.installation_id)" -cne "$($Runtime.InstallationId)" -or
            "$($state.version)" -cne "$($Runtime.Version)" -or
            -not (Test-PresenceSamePath -Left "$($state.source_root)" -Right "$($Runtime.SourceRoot)") -or
            -not (Test-PresenceSamePath -Left "$($state.python_executable)" -Right "$($Runtime.Pythonw)") -or
            -not [int]::TryParse("$($state.process_id)", [ref]$processId) -or
            $processId -le 0 -or
            "$($state.runtime_epoch)" -notmatch '^[0-9a-f]{32}$' -or
            [int]$state.port -ne [int]$Runtime.Port
        ) {
            throw "manual Presence state did not match this installation"
        }
        [void][DateTime]::Parse(
            "$($state.process_started_at)",
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind
        )
        return $state
    } catch {
        throw "Refusing to trust malformed or foreign manual Presence state: $($_.Exception.Message)"
    }
}

function Stop-ExactPresenceFromState {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)]$Runtime)

    $state = Get-PresenceManualState -Runtime $Runtime
    if ($null -eq $state) {
        return $null
    }
    $processId = [int]$state.process_id
    try {
        $process = Get-Process -Id $processId -ErrorAction Stop
    } catch {
        Remove-PresenceManualState -Runtime $Runtime
        return $null
    }
    $processPath = $null
    try { $processPath = "$($process.Path)" } catch { $processPath = $null }
    if (-not (Test-PresenceSamePath -Left $processPath -Right $Runtime.Pythonw)) {
        throw "Refusing to stop process $processId because the saved executable identity no longer matches."
    }
    $savedStart = [DateTime]::Parse(
        "$($state.process_started_at)",
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::RoundtripKind
    ).ToUniversalTime()
    $actualStart = $process.StartTime.ToUniversalTime()
    if ([Math]::Abs(($actualStart - $savedStart).TotalSeconds) -gt 0.001) {
        throw "Refusing to stop process $processId because its start time no longer matches the saved Presence instance."
    }
    Stop-Process -Id $processId -Force -ErrorAction Stop
    Wait-PresenceOffline -HealthUrl $Runtime.HealthUrl
    Remove-PresenceManualState -Runtime $Runtime
    return "$($state.runtime_epoch)"
}

function Write-PresenceManualState {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Runtime,
        [Parameter(Mandatory = $true)]$Health,
        [Parameter(Mandatory = $true)]$Process
    )

    Assert-PresenceHealthIdentity -Health $Health -Runtime $Runtime
    if ([int]$Health.process_id -ne [int]$Process.Id) {
        throw "The launched process ID did not match the healthy Presence endpoint."
    }
    $directory = Split-Path -Parent $Runtime.StateFile
    [IO.Directory]::CreateDirectory($directory) | Out-Null
    $state = [ordered]@{
        installation_id = $Runtime.InstallationId
        version = $Runtime.Version
        source_root = $Runtime.SourceRoot
        python_executable = $Runtime.Pythonw
        process_id = [int]$Process.Id
        process_started_at = $Process.StartTime.ToUniversalTime().ToString("o")
        runtime_epoch = "$($Health.runtime_epoch)"
        port = [int]$Runtime.Port
    }
    $temporary = "$($Runtime.StateFile).$([Guid]::NewGuid().ToString('N')).tmp"
    try {
        $state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temporary -Encoding UTF8
        Move-Item -LiteralPath $temporary -Destination $Runtime.StateFile -Force
    } finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
        }
    }
}

function Stop-ExactPresence {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Runtime,
        [AllowNull()]$Health = $null
    )

    if ($null -eq $Health) {
        $Health = Get-PresenceHealth -HealthUrl $Runtime.HealthUrl
    }
    $process = Get-ExactPresenceProcess -Health $Health -Runtime $Runtime
    $runtimeEpoch = "$($Health.runtime_epoch)"
    Stop-Process -Id $process.Id -Force -ErrorAction Stop
    Wait-PresenceOffline -HealthUrl $Runtime.HealthUrl
    Remove-PresenceManualState -Runtime $Runtime
    return $runtimeEpoch
}

function Resolve-PresenceTaskIdentity {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Project)

    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $currentUser = $identity.Name
    $sid = $identity.User.Value
    if (-not $currentUser -or -not $sid) {
        throw "Could not determine the current Windows user and SID."
    }
    $safeSid = $sid -replace '[^A-Za-z0-9-]', '-'
    return [pscustomobject]@{
        CurrentUser = $currentUser
        Sid = $sid
        TaskName = "JarvisLocalPresence-$safeSid"
        TaskPath = "\"
        Marker = "JARVIS_LOCAL_PRESENCE|SID=$sid|ROOT=$Project"
        Arguments = '-X utf8 -m jarvis presence --no-browser'
        MutexName = "Local\JarvisLocalPresenceLifecycle-$safeSid"
    }
}

function Resolve-PresenceAccountSid {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Account)

    try {
        if ($Account -match '^S-\d(?:-\d+)+$') {
            return ([System.Security.Principal.SecurityIdentifier]$Account).Value
        }
        $ntAccount = New-Object System.Security.Principal.NTAccount($Account)
        return ($ntAccount.Translate([System.Security.Principal.SecurityIdentifier])).Value
    } catch {
        throw "Could not resolve scheduled-task principal '$Account' to a Windows SID."
    }
}

function Assert-PresenceTaskOwnership {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Task,
        [Parameter(Mandatory = $true)]$TaskIdentity,
        [Parameter(Mandatory = $true)]$Runtime,
        [Parameter(Mandatory = $true)][string]$Project
    )

    if ("$($Task.TaskPath)" -cne "\") {
        throw "Refusing to modify '$($Task.TaskName)': it is not the owned root task."
    }
    if ("$($Task.Description)" -cne $TaskIdentity.Marker) {
        throw "Refusing to modify '$($Task.TaskName)' because Jarvis does not own it."
    }
    if ($null -eq $Task.Principal -or -not $Task.Principal.UserId) {
        throw "Refusing to modify '$($Task.TaskName)': it has no explicit user principal."
    }
    if ((Resolve-PresenceAccountSid -Account "$($Task.Principal.UserId)") -cne $TaskIdentity.Sid) {
        throw "Refusing to modify '$($Task.TaskName)': it belongs to another Windows user."
    }
    $actions = @($Task.Actions)
    if ($actions.Count -ne 1) {
        throw "Refusing to modify '$($Task.TaskName)': its action count is not owned by Jarvis."
    }
    if (-not (Test-PresenceSamePath -Left "$($actions[0].Execute)" -Right $Runtime.Pythonw)) {
        throw "Refusing to modify '$($Task.TaskName)': its Python executable does not match this installation."
    }
    if (-not (Test-PresenceSamePath -Left "$($actions[0].WorkingDirectory)" -Right $Project)) {
        throw "Refusing to modify '$($Task.TaskName)': its working directory belongs to another installation."
    }
    if ("$($actions[0].Arguments)" -cne $TaskIdentity.Arguments) {
        throw "Refusing to modify '$($Task.TaskName)': its Presence command does not match this installation."
    }
}
