Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$requiredCommands = @(
    "Export-ScheduledTask",
    "Get-ScheduledTask",
    "New-ScheduledTaskAction",
    "New-ScheduledTaskPrincipal",
    "New-ScheduledTaskSettingsSet",
    "New-ScheduledTaskTrigger",
    "Register-ScheduledTask",
    "Start-ScheduledTask",
    "Stop-ScheduledTask",
    "Unregister-ScheduledTask"
)
foreach ($commandName in $requiredCommands) {
    if (-not (Get-Command -Name $commandName -ErrorAction SilentlyContinue)) {
        throw "Required Windows Task Scheduler command '$commandName' is unavailable."
    }
}

function Invoke-NativeCommand {
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
        $detail = ""
        if ($output.Count -gt 0) {
            $detail = ": " + (($output | ForEach-Object { "$_".Trim() }) -join [Environment]::NewLine)
        }
        throw "$([IO.Path]::GetFileName($FilePath)) failed with exit code $exitCode$detail"
    }
    return $output
}

function Test-TaskNotFoundError {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][System.Management.Automation.ErrorRecord]$ErrorRecord)

    return (
        $ErrorRecord.CategoryInfo.Category -eq [System.Management.Automation.ErrorCategory]::ObjectNotFound -or
        $ErrorRecord.Exception -is [System.Management.Automation.ItemNotFoundException] -or
        "$($ErrorRecord.FullyQualifiedErrorId)" -match '(?i)(not.?found|no.?matching)' -or
        "$($ErrorRecord.Exception.Message)" -match '(?i)no\s+MSFT_ScheduledTask\s+objects?\s+found'
    )
}

function Get-TaskOrNull {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Name)

    try {
        $matches = @(Get-ScheduledTask -TaskName $Name -TaskPath "\" -ErrorAction Stop)
    } catch {
        if (Test-TaskNotFoundError -ErrorRecord $_) {
            return $null
        }
        throw
    }
    if ($matches.Count -gt 1) {
        throw "More than one root Task Scheduler entry matched '$Name'."
    }
    if ($matches.Count -eq 0) {
        return $null
    }
    return $matches[0]
}

function Test-SamePath {
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

function Resolve-AccountSid {
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

function Assert-TaskOwnership {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Task,
        [Parameter(Mandatory = $true)][string]$ExpectedDescription,
        [Parameter(Mandatory = $true)][string]$ExpectedSid,
        [Parameter(Mandatory = $true)][string]$ExpectedProject,
        [Parameter(Mandatory = $true)][string]$ExpectedPython,
        [Parameter(Mandatory = $true)][string]$ExpectedArguments
    )

    if ("$($Task.TaskPath)" -cne "\") {
        throw "Refusing to modify '$($Task.TaskName)': it is not the owned root task."
    }
    if ("$($Task.Description)" -cne $ExpectedDescription) {
        throw "Refusing to modify '$($Task.TaskName)' because Jarvis does not own it."
    }
    if ($null -eq $Task.Principal -or -not $Task.Principal.UserId) {
        throw "Refusing to modify '$($Task.TaskName)': it has no explicit user principal."
    }
    if ((Resolve-AccountSid -Account "$($Task.Principal.UserId)") -cne $ExpectedSid) {
        throw "Refusing to modify '$($Task.TaskName)': it belongs to another Windows user."
    }
    $actions = @($Task.Actions)
    if ($actions.Count -ne 1) {
        throw "Refusing to modify '$($Task.TaskName)': its action count is not owned by Jarvis."
    }
    if (-not (Test-SamePath -Left "$($actions[0].Execute)" -Right $ExpectedPython)) {
        throw "Refusing to modify '$($Task.TaskName)': its Python executable does not match this installation."
    }
    if (-not (Test-SamePath -Left "$($actions[0].WorkingDirectory)" -Right $ExpectedProject)) {
        throw "Refusing to modify '$($Task.TaskName)': its working directory belongs to another installation."
    }
    if ("$($actions[0].Arguments)" -cne $ExpectedArguments) {
        throw "Refusing to modify '$($Task.TaskName)': its Presence command does not match this installation."
    }
}

function Wait-TaskStopped {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Name, [int]$Attempts = 60)

    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        $task = Get-TaskOrNull -Name $Name
        if ($null -eq $task) {
            throw "Task '$Name' disappeared while it was being stopped."
        }
        if ("$($task.State)" -ine "Running") {
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Task '$Name' did not stop within 15 seconds."
}

function Test-PresenceHealth {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$HealthUrl, [switch]$RequireFresh)

    try {
        $response = Invoke-RestMethod -Uri $HealthUrl -Method Get -TimeoutSec 2 -ErrorAction Stop
        if ($response.service -ne "jarvis-presence" -or $response.ready -ne $true) {
            return $false
        }
        if ($RequireFresh) {
            $uptime = 0.0
            if (-not [double]::TryParse(
                "$($response.uptime_seconds)",
                [Globalization.NumberStyles]::Float,
                [Globalization.CultureInfo]::InvariantCulture,
                [ref]$uptime
            )) {
                return $false
            }
            return $uptime -ge 0 -and $uptime -le 45
        }
        return $true
    } catch {
        return $false
    }
}

function Wait-PresenceOffline {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$HealthUrl, [int]$Attempts = 60)

    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        if (-not (Test-PresenceHealth -HealthUrl $HealthUrl)) {
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "The previous Presence endpoint remained healthy after its scheduled task stopped; a fresh launch cannot be verified."
}

function Wait-PresenceHealth {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$HealthUrl, [int]$Attempts = 120)

    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        if (Test-PresenceHealth -HealthUrl $HealthUrl -RequireFresh) {
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Presence task did not produce a fresh healthy endpoint within 30 seconds."
}

function Get-PresenceRuntimeConfig {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$PythonPath)

    $configCode = @"
import json
import sys
from pathlib import Path
from jarvis.config import Config
config = Config.load()
pythonw = Path(sys.executable).with_name('pythonw.exe')
print('JARVIS_PRESENCE_CONFIG=' + json.dumps({'port': config.presence_port, 'pythonw': str(pythonw.resolve())}, ensure_ascii=True))
"@
    $output = @(Invoke-NativeCommand -FilePath $PythonPath -ArgumentList @("-X", "utf8", "-c", $configCode))
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
        return [pscustomobject]@{ Port = $port; Pythonw = $pythonw }
    } catch {
        throw "Jarvis returned malformed Presence runtime configuration: $($_.Exception.Message)"
    }
}

$pythonCommand = Get-Command -Name "python" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $pythonCommand) {
    throw "Python was not found. Install Python 3.11 or newer and make sure 'python' is on PATH."
}
# A scheduled task has no interactive console. Complete the one-time provider
# choice before registration so the first logon launch cannot get stuck or exit
# indefinitely waiting for setup.
Invoke-NativeCommand -FilePath $pythonCommand.Source -ArgumentList @(
    "-X", "utf8", "-m", "jarvis.provider_setup", "--interactive"
)
$runtime = Get-PresenceRuntimeConfig -PythonPath $pythonCommand.Source
$pythonw = $runtime.Pythonw
$port = $runtime.Port
$url = "http://127.0.0.1:$port/"
$health = "${url}api/health"

$project = [IO.Path]::GetFullPath($PSScriptRoot)
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$currentUser = $identity.Name
$sid = $identity.User.Value
if (-not $currentUser -or -not $sid) {
    throw "Could not determine the current Windows user and SID."
}
$safeSid = $sid -replace '[^A-Za-z0-9-]', '-'
$taskName = "JarvisLocalPresence-$safeSid"
$taskPath = "\"
$marker = "JARVIS_LOCAL_PRESENCE|SID=$sid|ROOT=$project"
$arguments = '-X utf8 -m jarvis presence --no-browser'
$mutexName = "Local\JarvisLocalPresenceLifecycle-$safeSid"
$lifecycleMutex = New-Object System.Threading.Mutex($false, $mutexName)
$mutexAcquired = $false

try {
    try {
        $mutexAcquired = $lifecycleMutex.WaitOne([TimeSpan]::Zero)
    } catch [System.Threading.AbandonedMutexException] {
        $mutexAcquired = $true
    }
    if (-not $mutexAcquired) {
        throw "Another Jarvis Presence install or uninstall is already in progress for this user."
    }

    $existing = Get-TaskOrNull -Name $taskName
    $backupXml = $null
    $previousWasRunning = $false
    if ($null -ne $existing) {
        Assert-TaskOwnership -Task $existing -ExpectedDescription $marker -ExpectedSid $sid -ExpectedProject $project -ExpectedPython $pythonw -ExpectedArguments $arguments
        $backupXml = [string](Export-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop)
        if (-not $backupXml.Trim()) {
            throw "Task Scheduler returned an empty backup for '$taskName'."
        }
        $previousWasRunning = "$($existing.State)" -ieq "Running"
    }

    $action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $project
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -RestartCount 255 `
        -RestartInterval ([TimeSpan]::FromMinutes(1)) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew

    $mutationStarted = $false
    try {
        if ($null -ne $existing -and $previousWasRunning) {
            $mutationStarted = $true
            Stop-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop
            Wait-TaskStopped -Name $taskName
            Wait-PresenceOffline -HealthUrl $health
        } elseif (Test-PresenceHealth -HealthUrl $health) {
            throw "The configured Presence port is already serving Jarvis outside the owned running task; a fresh launch cannot be verified."
        }

        $mutationStarted = $true
        Register-ScheduledTask `
            -TaskName $taskName `
            -TaskPath $taskPath `
            -Description $marker `
            -Action $action `
            -Trigger $trigger `
            -Principal $principal `
            -Settings $settings `
            -Force `
            -ErrorAction Stop | Out-Null

        $registered = Get-TaskOrNull -Name $taskName
        if ($null -eq $registered) {
            throw "Task Scheduler did not return the newly registered Presence task."
        }
        Assert-TaskOwnership -Task $registered -ExpectedDescription $marker -ExpectedSid $sid -ExpectedProject $project -ExpectedPython $pythonw -ExpectedArguments $arguments

        Start-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop
        Wait-PresenceHealth -HealthUrl $health
    } catch {
        $primaryError = $_
        $rollbackErrors = New-Object 'System.Collections.Generic.List[string]'
        if ($mutationStarted) {
            try {
                $current = Get-TaskOrNull -Name $taskName
                if ($null -ne $current) {
                    Assert-TaskOwnership -Task $current -ExpectedDescription $marker -ExpectedSid $sid -ExpectedProject $project -ExpectedPython $pythonw -ExpectedArguments $arguments
                    if ("$($current.State)" -ieq "Running") {
                        Stop-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop
                        Wait-TaskStopped -Name $taskName
                    }
                }
                if ($null -ne $backupXml) {
                    Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Xml $backupXml -Force -ErrorAction Stop | Out-Null
                    $restored = Get-TaskOrNull -Name $taskName
                    if ($null -eq $restored) {
                        throw "The previous Presence task could not be restored."
                    }
                    Assert-TaskOwnership -Task $restored -ExpectedDescription $marker -ExpectedSid $sid -ExpectedProject $project -ExpectedPython $pythonw -ExpectedArguments $arguments
                    if ($previousWasRunning) {
                        Start-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop
                    }
                } elseif ($null -ne $current) {
                    Unregister-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Confirm:$false -ErrorAction Stop
                    if ($null -ne (Get-TaskOrNull -Name $taskName)) {
                        throw "The failed Presence task is still registered."
                    }
                }
            } catch {
                [void]$rollbackErrors.Add($_.Exception.Message)
            }
        }
        if ($rollbackErrors.Count -gt 0) {
            throw "Presence installation failed: $($primaryError.Exception.Message) Rollback also failed: $($rollbackErrors -join '; ')"
        }
        throw $primaryError
    }

    Write-Host "Jarvis Presence starts automatically when you sign in."
    Write-Host "Open $url"
} finally {
    if ($mutexAcquired) {
        [void]$lifecycleMutex.ReleaseMutex()
    }
    $lifecycleMutex.Dispose()
}
