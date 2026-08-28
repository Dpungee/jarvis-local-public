param(
    [ValidateSet("start", "status", "restart", "stop")]
    [string]$Action = "start",
    [switch]$NoBrowser
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$project = [IO.Path]::GetFullPath($PSScriptRoot)
. (Join-Path $project "presence_lifecycle.ps1")

$pythonCommand = Get-Command -Name "python" -CommandType Application -ErrorAction Stop |
    Select-Object -First 1

if ($Action -in @("start", "restart")) {
    & $pythonCommand.Source -X utf8 -m jarvis.provider_setup --interactive
    $setupExit = $LASTEXITCODE
    if ($null -eq $setupExit -or $setupExit -ne 0) {
        exit $(if ($null -eq $setupExit) { 1 } else { $setupExit })
    }
}

$runtime = Get-PresenceRuntimeConfig -PythonPath $pythonCommand.Source
$taskIdentity = Resolve-PresenceTaskIdentity -Project $project

function Get-OwnedPresenceTask {
    try {
        $tasks = @(Get-ScheduledTask `
            -TaskName $taskIdentity.TaskName `
            -TaskPath $taskIdentity.TaskPath `
            -ErrorAction Stop)
    } catch {
        if (
            $_.CategoryInfo.Category -eq [System.Management.Automation.ErrorCategory]::ObjectNotFound -or
            $_.Exception -is [System.Management.Automation.ItemNotFoundException] -or
            "$($_.FullyQualifiedErrorId)" -match '(?i)(not.?found|no.?matching)' -or
            "$($_.Exception.Message)" -match '(?i)no\s+MSFT_ScheduledTask\s+objects?\s+found'
        ) {
            return $null
        }
        throw
    }
    if ($tasks.Count -gt 1) {
        throw "More than one root Task Scheduler entry matched '$($taskIdentity.TaskName)'."
    }
    if ($tasks.Count -eq 0) {
        return $null
    }
    Assert-PresenceTaskOwnership `
        -Task $tasks[0] `
        -TaskIdentity $taskIdentity `
        -Runtime $runtime `
        -Project $project
    return $tasks[0]
}

function Stop-CurrentPresence {
    $health = Get-PresenceHealth -HealthUrl $runtime.HealthUrl
    if ($null -ne $health) {
        Assert-PresenceHealthIdentity -Health $health -Runtime $runtime
    }
    $task = Get-OwnedPresenceTask
    if ($null -ne $task -and "$($task.State)" -ieq "Running") {
        $previousEpoch = if ($null -eq $health) { $null } else { "$($health.runtime_epoch)" }
        Stop-ScheduledTask `
            -TaskName $taskIdentity.TaskName `
            -TaskPath $taskIdentity.TaskPath `
            -ErrorAction Stop
        Wait-PresenceOffline -HealthUrl $runtime.HealthUrl
        Remove-PresenceManualState -Runtime $runtime
        return $previousEpoch
    }
    if ($null -ne $health) {
        return Stop-ExactPresence -Runtime $runtime -Health $health
    }
    return Stop-ExactPresenceFromState -Runtime $runtime
}

function Start-CurrentPresence {
    param([AllowNull()][string]$PreviousRuntimeEpoch = $null)

    $health = Get-PresenceHealth -HealthUrl $runtime.HealthUrl
    if ($null -ne $health) {
        Assert-PresenceHealthIdentity -Health $health -Runtime $runtime
        return $health
    }

    $task = Get-OwnedPresenceTask
    if ($null -ne $task) {
        Start-ScheduledTask `
            -TaskName $taskIdentity.TaskName `
            -TaskPath $taskIdentity.TaskPath `
            -ErrorAction Stop
        return Wait-PresenceHealth `
            -Runtime $runtime `
            -PreviousRuntimeEpoch $PreviousRuntimeEpoch
    }

    $oldLaunchMode = $env:JARVIS_PRESENCE_LAUNCH_MODE
    $process = $null
    try {
        $env:JARVIS_PRESENCE_LAUNCH_MODE = "manual"
        $process = Start-Process `
            -FilePath $runtime.Pythonw `
            -ArgumentList @("-X", "utf8", "-m", "jarvis", "presence", "--no-browser") `
            -WorkingDirectory $project `
            -WindowStyle Hidden `
            -PassThru
    } finally {
        if ($null -eq $oldLaunchMode) {
            Remove-Item Env:JARVIS_PRESENCE_LAUNCH_MODE -ErrorAction SilentlyContinue
        } else {
            $env:JARVIS_PRESENCE_LAUNCH_MODE = $oldLaunchMode
        }
    }

    try {
        $health = Wait-PresenceHealth `
            -Runtime $runtime `
            -PreviousRuntimeEpoch $PreviousRuntimeEpoch
        if ("$($health.launch_mode)" -cne "manual") {
            throw "The new Presence process did not identify itself as a manual launch."
        }
        Write-PresenceManualState -Runtime $runtime -Health $health -Process $process
        return $health
    } catch {
        if ($null -ne $process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
        Remove-PresenceManualState -Runtime $runtime
        throw
    }
}

$lifecycleMutex = New-Object System.Threading.Mutex($false, $taskIdentity.MutexName)
$mutexAcquired = $false
try {
    try {
        $mutexAcquired = $lifecycleMutex.WaitOne([TimeSpan]::Zero)
    } catch [System.Threading.AbandonedMutexException] {
        $mutexAcquired = $true
    }
    if (-not $mutexAcquired) {
        throw "Another Jarvis Presence lifecycle action is already in progress for this user."
    }

    switch ($Action) {
        "status" {
            $health = Get-PresenceHealth -HealthUrl $runtime.HealthUrl
            if ($null -eq $health) {
                throw "JARVIS Presence is offline for this installation."
            }
            Assert-PresenceHealthIdentity -Health $health -Runtime $runtime
            Write-Host (
                "JARVIS Presence is online at {0} (version {1}, PID {2}, mode {3}, epoch {4})." -f
                $runtime.Url, $health.version, $health.process_id, $health.launch_mode, $health.runtime_epoch
            )
            return
        }
        "stop" {
            $previousEpoch = Stop-CurrentPresence
            if ($null -eq $previousEpoch) {
                Write-Host "JARVIS Presence was already offline for this installation."
            } else {
                Write-Host "JARVIS Presence stopped."
            }
            return
        }
        "restart" {
            $previousEpoch = Stop-CurrentPresence
            $health = Start-CurrentPresence -PreviousRuntimeEpoch $previousEpoch
            Write-Host "JARVIS Presence restarted with epoch $($health.runtime_epoch)."
        }
        default {
            $health = Start-CurrentPresence
        }
    }

    if (-not $NoBrowser) {
        Start-Process $runtime.Url
    }
    Write-Host "JARVIS Presence is online at $($runtime.Url)"
} finally {
    if ($mutexAcquired) {
        [void]$lifecycleMutex.ReleaseMutex()
    }
    $lifecycleMutex.Dispose()
}
