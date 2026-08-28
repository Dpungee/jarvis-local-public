Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$project = (Resolve-Path -LiteralPath $PSScriptRoot).Path
. (Join-Path $project "presence_lifecycle.ps1")

$pythonCommand = Get-Command -Name "python" -CommandType Application -ErrorAction Stop |
    Select-Object -First 1
$runtime = Get-PresenceRuntimeConfig -PythonPath $pythonCommand.Source
$taskIdentity = Resolve-PresenceTaskIdentity -Project $project
$lifecycleMutex = New-Object System.Threading.Mutex($false, $taskIdentity.MutexName)
$mutexAcquired = $false

function Get-TaskOrNull {
    try {
        $matches = @(Get-ScheduledTask `
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
    if ($matches.Count -gt 1) {
        throw "More than one root Task Scheduler entry matched '$($taskIdentity.TaskName)'."
    }
    if ($matches.Count -eq 0) {
        return $null
    }
    return $matches[0]
}

try {
    try {
        $mutexAcquired = $lifecycleMutex.WaitOne([TimeSpan]::Zero)
    } catch [System.Threading.AbandonedMutexException] {
        $mutexAcquired = $true
    }
    if (-not $mutexAcquired) {
        throw "Another Jarvis Presence install or uninstall is already in progress for this user."
    }

    $existing = Get-TaskOrNull
    if ($null -ne $existing) {
        try {
            Assert-PresenceTaskOwnership `
                -Task $existing `
                -TaskIdentity $taskIdentity `
                -Runtime $runtime `
                -Project $project
        } catch {
            throw "Refusing to remove scheduled task '$($taskIdentity.TaskName)': $($_.Exception.Message)"
        }
    }

    $health = Get-PresenceHealth -HealthUrl $runtime.HealthUrl
    if ($null -ne $health) {
        Assert-PresenceHealthIdentity -Health $health -Runtime $runtime
    }

    if ($null -ne $existing -and "$($existing.State)" -ieq "Running") {
        Stop-ScheduledTask `
            -TaskName $taskIdentity.TaskName `
            -TaskPath $taskIdentity.TaskPath `
            -ErrorAction Stop
        Wait-PresenceOffline -HealthUrl $runtime.HealthUrl
    } elseif ($null -ne $health) {
        # A manually launched instance is stopped only after the health identity,
        # PID, and executable have all been matched to this exact installation.
        Stop-ExactPresence -Runtime $runtime -Health $health | Out-Null
    } else {
        Stop-ExactPresenceFromState -Runtime $runtime | Out-Null
    }

    if ($null -ne $existing) {
        Unregister-ScheduledTask `
            -TaskName $taskIdentity.TaskName `
            -TaskPath $taskIdentity.TaskPath `
            -Confirm:$false `
            -ErrorAction Stop
        if ($null -ne (Get-TaskOrNull)) {
            throw "The Presence scheduled task still exists after uninstall."
        }
    }
    Remove-PresenceManualState -Runtime $runtime

    if ($null -eq $existing -and $null -eq $health) {
        Write-Host "Jarvis Presence was already stopped and automatic startup was not installed."
    } else {
        Write-Host "Jarvis Presence stopped and automatic startup was removed. Data and conversations were preserved."
    }
} finally {
    if ($mutexAcquired) {
        [void]$lifecycleMutex.ReleaseMutex()
    }
    $lifecycleMutex.Dispose()
}
