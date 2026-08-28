Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$requiredCommands = @(
    "Export-ScheduledTask",
    "Get-ScheduledTask",
    "Get-ScheduledTaskInfo",
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
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [string[]]$ArgumentList = @(),
        [switch]$CaptureOutput
    )

    $output = @()
    if ($CaptureOutput) {
        $output = @(& $FilePath @ArgumentList 2>&1)
    } else {
        & $FilePath @ArgumentList
    }
    $exitCode = $LASTEXITCODE
    if ($null -eq $exitCode) {
        throw "Could not determine the exit code from $FilePath."
    }
    if ($exitCode -ne 0) {
        $detail = ""
        if ($CaptureOutput -and $output.Count -gt 0) {
            $detail = ": " + (($output | ForEach-Object { "$_".Trim() }) -join [Environment]::NewLine)
        }
        throw "$([IO.Path]::GetFileName($FilePath)) failed with exit code $exitCode$detail"
    }
    if ($CaptureOutput) {
        return $output
    }
}

function Test-TaskNotFoundError {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [System.Management.Automation.ErrorRecord]$ErrorRecord
    )

    return (
        $ErrorRecord.CategoryInfo.Category -eq [System.Management.Automation.ErrorCategory]::ObjectNotFound -or
        $ErrorRecord.Exception -is [System.Management.Automation.ItemNotFoundException] -or
        "$($ErrorRecord.FullyQualifiedErrorId)" -match '(?i)(not.?found|no.?matching)' -or
        "$($ErrorRecord.Exception.Message)" -match '(?i)no\s+MSFT_ScheduledTask\s+objects?\s+found'
    )
}

function Get-TaskOrNull {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

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
    param(
        [AllowNull()]
        [string]$Left,
        [AllowNull()]
        [string]$Right
    )

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
    param(
        [Parameter(Mandatory = $true)]
        [string]$Account
    )

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
        [Parameter(Mandatory = $true)][string]$ExpectedArguments
    )

    if ("$($Task.TaskPath)" -cne "\") {
        throw "Refusing to modify '$($Task.TaskName)': it is not the owned root task."
    }
    if ("$($Task.Description)" -cne $ExpectedDescription) {
        throw "Refusing to modify '$($Task.TaskName)': its ownership description does not match this JARVIS installation."
    }
    if ($null -eq $Task.Principal -or -not $Task.Principal.UserId) {
        throw "Refusing to modify '$($Task.TaskName)': it has no explicit user principal."
    }
    $principalSid = Resolve-AccountSid -Account "$($Task.Principal.UserId)"
    if ($principalSid -cne $ExpectedSid) {
        throw "Refusing to modify '$($Task.TaskName)': it belongs to a different Windows user."
    }

    $actions = @($Task.Actions)
    if ($actions.Count -ne 1) {
        throw "Refusing to modify '$($Task.TaskName)': its action count is not owned by JARVIS."
    }
    if (-not (Test-SamePath -Left "$($actions[0].WorkingDirectory)" -Right $ExpectedProject)) {
        throw "Refusing to modify '$($Task.TaskName)': its working directory belongs to another installation."
    }
    if ("$($actions[0].Arguments)" -cne $ExpectedArguments) {
        throw "Refusing to modify '$($Task.TaskName)': its worker command does not match this installation."
    }
}

function Assert-TaskDefinition {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Task,
        [Parameter(Mandatory = $true)][string]$ExpectedDescription,
        [Parameter(Mandatory = $true)][string]$ExpectedSid,
        [Parameter(Mandatory = $true)][string]$ExpectedProject,
        [Parameter(Mandatory = $true)][string]$ExpectedPython,
        [Parameter(Mandatory = $true)][string]$ExpectedArguments
    )

    Assert-TaskOwnership -Task $Task -ExpectedDescription $ExpectedDescription -ExpectedSid $ExpectedSid -ExpectedProject $ExpectedProject -ExpectedArguments $ExpectedArguments
    $action = @($Task.Actions)[0]
    if (-not (Test-SamePath -Left "$($action.Execute)" -Right $ExpectedPython)) {
        throw "The registered worker action does not use the selected Python executable."
    }
    if ("$($Task.Principal.LogonType)" -notmatch '(?i)^Interactive') {
        throw "The registered worker principal is not interactive."
    }
    if ("$($Task.Principal.RunLevel)" -ine "Limited") {
        throw "The registered worker unexpectedly requests elevation."
    }

    $triggers = @($Task.Triggers)
    if ($triggers.Count -ne 2) {
        throw "The registered worker must have one logon trigger and one watchdog trigger."
    }
    $hasLogonTrigger = $false
    $hasWatchdogTrigger = $false
    foreach ($trigger in $triggers) {
        if ($trigger.PSObject.Properties.Name -contains "UserId" -and $trigger.UserId) {
            $hasLogonTrigger = $true
        }
        if (
            $trigger.PSObject.Properties.Name -contains "Repetition" -and
            $null -ne $trigger.Repetition -and
            $trigger.Repetition.Interval
        ) {
            $hasWatchdogTrigger = $true
        }
    }
    if (-not $hasLogonTrigger -or -not $hasWatchdogTrigger) {
        throw "The registered worker is missing its user-logon or periodic-watchdog trigger."
    }

    $settings = $Task.Settings
    if ($null -eq $settings) {
        throw "The registered worker has no reliability settings."
    }
    if ([int]$settings.RestartCount -ne 255) {
        throw "The registered worker restart count is not 255."
    }
    if ("$($settings.MultipleInstances)" -ine "IgnoreNew") {
        throw "The registered worker does not ignore overlapping starts."
    }
    foreach ($property in @("StartWhenAvailable", "WakeToRun")) {
        if (-not [bool]$settings.$property) {
            throw "The registered worker setting '$property' is disabled."
        }
    }
    $settingNames = @($settings.PSObject.Properties.Name)
    if ($settingNames -contains "AllowStartIfOnBatteries") {
        if (-not [bool]$settings.AllowStartIfOnBatteries) {
            throw "The registered worker is not allowed to start on batteries."
        }
    } elseif ($settingNames -contains "DisallowStartIfOnBatteries") {
        if ([bool]$settings.DisallowStartIfOnBatteries) {
            throw "The registered worker is not allowed to start on batteries."
        }
    } else {
        throw "The registered worker exposes no battery-start setting."
    }
    if ($settingNames -contains "DontStopIfGoingOnBatteries") {
        if (-not [bool]$settings.DontStopIfGoingOnBatteries) {
            throw "The registered worker stops when switching to batteries."
        }
    } elseif ($settingNames -contains "StopIfGoingOnBatteries") {
        if ([bool]$settings.StopIfGoingOnBatteries) {
            throw "The registered worker stops when switching to batteries."
        }
    } else {
        throw "The registered worker exposes no battery-stop setting."
    }
    $executionLimit = "$($settings.ExecutionTimeLimit)"
    if ($executionLimit -notin @("00:00:00", "PT0S")) {
        throw "The registered worker has a finite execution-time limit."
    }
}

function Wait-TaskStopped {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [int]$Attempts = 60
    )

    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        $current = Get-TaskOrNull -Name $Name
        if ($null -eq $current) {
            throw "Task '$Name' disappeared while it was being stopped."
        }
        if ("$($current.State)" -ine "Running") {
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Task '$Name' did not stop within 15 seconds."
}

function Wait-WorkerLockReleased {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][string]$DataDirectory,
        [int]$Attempts = 60
    )

    $probeCode = @"
import sys
from pathlib import Path
from jarvis.cli import _WorkerProcessLock
lock = _WorkerProcessLock(Path(sys.argv[1]))
acquired = lock.acquire()
if acquired:
    lock.close()
raise SystemExit(0 if acquired else 3)
"@
    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        $output = @(& $PythonPath -X utf8 -c $probeCode $DataDirectory 2>&1)
        $exitCode = $LASTEXITCODE
        if ($exitCode -eq 0) {
            return
        }
        if ($exitCode -ne 3) {
            $detail = (($output | ForEach-Object { "$_".Trim() }) -join [Environment]::NewLine)
            throw "Worker lock probe failed with exit code $exitCode`: $detail"
        }
        Start-Sleep -Milliseconds 250
    }
    throw "The previous continuous worker did not release its process lock within 15 seconds."
}

function Wait-WorkerHeartbeat {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$HeartbeatPath,
        [Parameter(Mandatory = $true)][long]$NotBefore,
        [int]$Attempts = 30
    )

    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        if (Test-Path -LiteralPath $HeartbeatPath -PathType Leaf) {
            try {
                $heartbeat = (Get-Content -LiteralPath $HeartbeatPath -Raw -ErrorAction Stop).Trim()
                $parts = @($heartbeat -split '\s+')
                $timestamp = 0.0
                $pidValue = 0
                $validTime = $parts.Count -ge 3 -and [double]::TryParse(
                    $parts[0],
                    [Globalization.NumberStyles]::Float,
                    [Globalization.CultureInfo]::InvariantCulture,
                    [ref]$timestamp
                )
                $validPid = $parts.Count -ge 3 -and [int]::TryParse($parts[1], [ref]$pidValue) -and $pidValue -gt 0
                $fresh = $validTime -and $timestamp -ge ($NotBefore - 1) -and $timestamp -le ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() + 5)
                if ($fresh -and $validPid -and $parts[2].StartsWith("worker:", [StringComparison]::Ordinal)) {
                    $task = Get-TaskOrNull -Name $Name
                    if ($null -ne $task -and "$($task.State)" -ieq "Running") {
                        return
                    }
                }
            } catch {
                # A concurrent atomic replace can briefly race with this read; retry.
            }
        }
        Start-Sleep -Seconds 1
    }

    $state = "missing"
    $lastResult = "unknown"
    $task = Get-TaskOrNull -Name $Name
    if ($null -ne $task) {
        $state = "$($task.State)"
        try {
            $taskInfo = Get-ScheduledTaskInfo -TaskName $Name -TaskPath "\" -ErrorAction Stop
            $lastResult = "$($taskInfo.LastTaskResult)"
        } catch {
            $lastResult = "unavailable"
        }
    }
    throw "The worker did not produce a fresh startup heartbeat (state $state, last result $lastResult)."
}

function Get-JarvisRuntimeConfig {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$PythonPath)

    $configCode = @"
import json
from jarvis.config import Config
config = Config.load()
print('JARVIS_RUNTIME_CONFIG=' + json.dumps({'data_dir': str(config.data_dir.resolve())}, ensure_ascii=True))
"@
    $output = @(Invoke-NativeCommand -FilePath $PythonPath -ArgumentList @(
        "-X", "utf8", "-c", $configCode
    ) -CaptureOutput)
    $prefix = "JARVIS_RUNTIME_CONFIG="
    $line = $output |
        ForEach-Object { "$_".Trim() } |
        Where-Object { $_.StartsWith($prefix, [StringComparison]::Ordinal) } |
        Select-Object -Last 1
    if (-not $line) {
        throw "JARVIS did not return its runtime data directory."
    }
    try {
        $decoded = $line.Substring($prefix.Length) | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $decoded.data_dir -or -not ($decoded.data_dir -is [string]) -or -not $decoded.data_dir.Trim()) {
            throw "data_dir was absent"
        }
        return [IO.Path]::GetFullPath($decoded.data_dir)
    } catch {
        throw "JARVIS returned malformed runtime configuration JSON: $($_.Exception.Message)"
    }
}

$pythonCommand = Get-Command -Name "python" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $pythonCommand) {
    throw "Python was not found. Install Python 3.11 or newer and make sure 'python' is on PATH."
}
$python = $pythonCommand.Source

$versionOutput = @(Invoke-NativeCommand -FilePath $python -ArgumentList @(
    "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"
) -CaptureOutput)
$versionText = $versionOutput | ForEach-Object { "$_".Trim() } | Where-Object { $_ -match '^\d+\.\d+\.\d+$' } | Select-Object -Last 1
if (-not $versionText) {
    throw "Could not determine the Python version."
}
$pythonVersion = [version]$versionText
if ($pythonVersion -lt [version]"3.11") {
    throw "Python 3.11 or newer is required; found $pythonVersion."
}

Write-Host "Checking JARVIS before installing the worker..."
Invoke-NativeCommand -FilePath $python -ArgumentList @(
    "-X", "utf8", "-m", "jarvis.provider_setup", "--interactive"
)
Invoke-NativeCommand -FilePath $python -ArgumentList @("-X", "utf8", "-m", "jarvis", "doctor")
$dataDirectory = Get-JarvisRuntimeConfig -PythonPath $python

$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$currentUser = $identity.Name
$currentSid = $identity.User.Value
if (-not $currentUser -or -not $currentSid) {
    throw "Could not determine the current Windows user and SID."
}

$project = [IO.Path]::GetFullPath($PSScriptRoot)
$logDir = $dataDirectory
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir "worker.log"
$heartbeatPath = Join-Path $logDir "worker.heartbeat"
if ($python.Contains('"') -or $project.Contains('"') -or $logDir.Contains('"') -or $logPath.Contains('"')) {
    throw "The worker cannot be installed from a path containing a double quote."
}

$sidToken = $currentSid -replace '[^A-Za-z0-9_.-]', '_'
$taskName = "JarvisLocalWorker-$sidToken"
$taskPath = "\"
$description = "JARVIS_LOCAL_WORKER|SID=$currentSid|ROOT=$project|DATA=$logDir"
if ($description.Length -gt 1024) {
    throw "The JARVIS installation and data paths are too long for a Task Scheduler description."
}
$workerArguments = '-X utf8 -u -m jarvis worker --log "{0}"' -f $logPath
$mutexName = "Local\JarvisLocalWorkerLifecycle-$sidToken"
$lifecycleMutex = New-Object System.Threading.Mutex($false, $mutexName)
$mutexAcquired = $false
try {
    try {
        $mutexAcquired = $lifecycleMutex.WaitOne([TimeSpan]::Zero)
    } catch [System.Threading.AbandonedMutexException] {
        $mutexAcquired = $true
    }
    if (-not $mutexAcquired) {
        throw "Another JARVIS worker install or uninstall is already in progress for this user."
    }

    $existing = Get-TaskOrNull -Name $taskName
    $backupXml = $null
    $previousWasRunning = $false
    if ($null -ne $existing) {
        Assert-TaskOwnership -Task $existing -ExpectedDescription $description -ExpectedSid $currentSid -ExpectedProject $project -ExpectedArguments $workerArguments
        $backupXml = [string](Export-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop)
        if (-not $backupXml.Trim()) {
            throw "Task Scheduler returned an empty backup for '$taskName'."
        }
        $previousWasRunning = "$($existing.State)" -ieq "Running"
    }

    $action = New-ScheduledTaskAction -Execute $python -Argument $workerArguments -WorkingDirectory $project
    $logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
    $watchdogTrigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(5)) -RepetitionInterval (New-TimeSpan -Minutes 15)
    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 255 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -WakeToRun
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited

    $mutationStarted = $false
    try {
        if ($null -ne $existing -and $previousWasRunning) {
            $mutationStarted = $true
            Stop-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop
            Wait-TaskStopped -Name $taskName
            Wait-WorkerLockReleased -PythonPath $python -DataDirectory $dataDirectory
        }

        $mutationStarted = $true
        Register-ScheduledTask `
            -TaskName $taskName `
            -TaskPath $taskPath `
            -Action $action `
            -Trigger @($logonTrigger, $watchdogTrigger) `
            -Settings $settings `
            -Principal $principal `
            -Description $description `
            -Force `
            -ErrorAction Stop | Out-Null

        $registered = Get-TaskOrNull -Name $taskName
        if ($null -eq $registered) {
            throw "Task Scheduler did not return the newly registered worker."
        }
        Assert-TaskDefinition -Task $registered -ExpectedDescription $description -ExpectedSid $currentSid -ExpectedProject $project -ExpectedPython $python -ExpectedArguments $workerArguments

        if (Test-Path -LiteralPath $heartbeatPath) {
            Remove-Item -LiteralPath $heartbeatPath -Force -ErrorAction Stop
        }
        $launchNotBefore = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        Start-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop
        Wait-WorkerHeartbeat -Name $taskName -HeartbeatPath $heartbeatPath -NotBefore $launchNotBefore
    } catch {
        $primaryError = $_
        $rollbackErrors = New-Object 'System.Collections.Generic.List[string]'
        if ($mutationStarted) {
            try {
                $current = Get-TaskOrNull -Name $taskName
                if ($null -ne $current) {
                    Assert-TaskOwnership -Task $current -ExpectedDescription $description -ExpectedSid $currentSid -ExpectedProject $project -ExpectedArguments $workerArguments
                    if ("$($current.State)" -ieq "Running") {
                        Stop-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop
                        Wait-TaskStopped -Name $taskName
                        Wait-WorkerLockReleased -PythonPath $python -DataDirectory $dataDirectory
                    }
                }

                if ($null -ne $backupXml) {
                    Register-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Xml $backupXml -Force -ErrorAction Stop | Out-Null
                    $restored = Get-TaskOrNull -Name $taskName
                    if ($null -eq $restored) {
                        throw "The previous worker task could not be restored."
                    }
                    Assert-TaskOwnership -Task $restored -ExpectedDescription $description -ExpectedSid $currentSid -ExpectedProject $project -ExpectedArguments $workerArguments
                    if ($previousWasRunning) {
                        Start-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop
                    }
                } elseif ($null -ne $current) {
                    Unregister-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Confirm:$false -ErrorAction Stop
                    if ($null -ne (Get-TaskOrNull -Name $taskName)) {
                        throw "The failed worker task is still registered."
                    }
                }
            } catch {
                [void]$rollbackErrors.Add($_.Exception.Message)
            }
        }

        if ($rollbackErrors.Count -gt 0) {
            throw "Worker installation failed: $($primaryError.Exception.Message) Rollback also failed: $($rollbackErrors -join '; ')"
        }
        throw $primaryError
    }

    Write-Host "JARVIS background worker installed and started as '$taskName'."
    Write-Host "It can run only while $currentUser is signed in; a watchdog checks it every 15 minutes."
    Write-Host "Log: $logPath"
} finally {
    if ($mutexAcquired) {
        [void]$lifecycleMutex.ReleaseMutex()
    }
    $lifecycleMutex.Dispose()
}
