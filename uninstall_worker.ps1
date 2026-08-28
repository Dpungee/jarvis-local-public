Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

foreach ($commandName in @("Get-ScheduledTask", "Stop-ScheduledTask", "Unregister-ScheduledTask")) {
    if (-not (Get-Command -Name $commandName -ErrorAction SilentlyContinue)) {
        throw "Required Windows Task Scheduler command '$commandName' is unavailable."
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
        [AllowNull()][string]$Left,
        [AllowNull()][string]$Right
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

function Get-OwnedTaskMetadata {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Task,
        [Parameter(Mandatory = $true)][string]$ExpectedSid,
        [Parameter(Mandatory = $true)][string]$ExpectedProject
    )

    $description = "$($Task.Description)"
    $match = [regex]::Match(
        $description,
        '^JARVIS_LOCAL_WORKER\|SID=([^|]+)\|ROOT=([^|]+)\|DATA=(.+)$',
        [Text.RegularExpressions.RegexOptions]::CultureInvariant
    )
    if (-not $match.Success) {
        throw "Refusing to remove '$($Task.TaskName)': its ownership description is malformed."
    }
    if ($match.Groups[1].Value -cne $ExpectedSid) {
        throw "Refusing to remove '$($Task.TaskName)': its ownership SID does not match this Windows user."
    }
    if (-not (Test-SamePath -Left $match.Groups[2].Value -Right $ExpectedProject)) {
        throw "Refusing to remove '$($Task.TaskName)': its ownership root belongs to another installation."
    }
    try {
        $dataDirectory = [IO.Path]::GetFullPath($match.Groups[3].Value)
    } catch {
        throw "Refusing to remove '$($Task.TaskName)': its data directory is invalid."
    }
    if (-not [IO.Path]::IsPathRooted($dataDirectory) -or $dataDirectory.Contains('"')) {
        throw "Refusing to remove '$($Task.TaskName)': its data directory is unsafe."
    }
    return [pscustomobject]@{
        Description = $description
        DataDirectory = $dataDirectory
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
        throw "Refusing to remove '$($Task.TaskName)': it is not the owned root task."
    }
    if ("$($Task.Description)" -cne $ExpectedDescription) {
        throw "Refusing to remove '$($Task.TaskName)': its ownership description does not match this JARVIS installation."
    }
    if ($null -eq $Task.Principal -or -not $Task.Principal.UserId) {
        throw "Refusing to remove '$($Task.TaskName)': it has no explicit user principal."
    }
    $principalSid = Resolve-AccountSid -Account "$($Task.Principal.UserId)"
    if ($principalSid -cne $ExpectedSid) {
        throw "Refusing to remove '$($Task.TaskName)': it belongs to a different Windows user."
    }

    $actions = @($Task.Actions)
    if ($actions.Count -ne 1) {
        throw "Refusing to remove '$($Task.TaskName)': its action count is not owned by JARVIS."
    }
    if (-not (Test-SamePath -Left "$($actions[0].WorkingDirectory)" -Right $ExpectedProject)) {
        throw "Refusing to remove '$($Task.TaskName)': its working directory belongs to another installation."
    }
    if ("$($actions[0].Arguments)" -cne $ExpectedArguments) {
        throw "Refusing to remove '$($Task.TaskName)': its worker command does not match this installation."
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

$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$currentUser = $identity.Name
$currentSid = $identity.User.Value
if (-not $currentUser -or -not $currentSid) {
    throw "Could not determine the current Windows user and SID."
}

$project = [IO.Path]::GetFullPath($PSScriptRoot)
$sidToken = $currentSid -replace '[^A-Za-z0-9_.-]', '_'
$taskName = "JarvisLocalWorker-$sidToken"
$taskPath = "\"
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

    $task = Get-TaskOrNull -Name $taskName
    if ($null -eq $task) {
        Write-Host "JARVIS background worker is not installed for $currentUser."
        return
    }

    $metadata = Get-OwnedTaskMetadata -Task $task -ExpectedSid $currentSid -ExpectedProject $project
    $logPath = Join-Path $metadata.DataDirectory "worker.log"
    $workerArguments = '-X utf8 -u -m jarvis worker --log "{0}"' -f $logPath
    Assert-TaskOwnership -Task $task -ExpectedDescription $metadata.Description -ExpectedSid $currentSid -ExpectedProject $project -ExpectedArguments $workerArguments
    if ("$($task.State)" -ieq "Running") {
        Stop-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction Stop
        Wait-TaskStopped -Name $taskName
    }

    Unregister-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Confirm:$false -ErrorAction Stop
    $removed = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if ($null -eq (Get-TaskOrNull -Name $taskName)) {
            $removed = $true
            break
        }
        Start-Sleep -Milliseconds 250
    }
    if (-not $removed) {
        throw "Task Scheduler still reports '$taskName' after removal."
    }

    Write-Host "JARVIS background worker removed for $currentUser. Your memory, logs, and files were kept."
} finally {
    if ($mutexAcquired) {
        [void]$lifecycleMutex.ReleaseMutex()
    }
    $lifecycleMutex.Dispose()
}
