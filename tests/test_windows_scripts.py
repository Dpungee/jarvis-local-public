import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
COMSPEC = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
TEST_TEMP_ROOT = ROOT / "tests" / ".tmp"


SCHEDULER_HARNESS = r"""
Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$script:Scenario = $env:JARVIS_TEST_SCENARIO
$script:TracePath = $env:JARVIS_TEST_TRACE
$script:Project = [IO.Path]::GetFullPath($env:JARVIS_TEST_PROJECT)
$script:TaskScript = $env:JARVIS_TEST_SCRIPT
$script:ResultPath = $env:JARVIS_TEST_RESULT
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$script:CurrentUser = $identity.Name
$script:CurrentSid = $identity.User.Value
$script:SidToken = $script:CurrentSid -replace '[^A-Za-z0-9_.-]', '_'
$script:TaskName = "JarvisLocalWorker-$script:SidToken"
$script:DataDirectory = [IO.Path]::GetFullPath($env:JARVIS_TEST_DATA)
$script:Description = "JARVIS_LOCAL_WORKER|SID=$script:CurrentSid|ROOT=$script:Project|DATA=$script:DataDirectory"
$script:Python = (Get-Command python -CommandType Application | Select-Object -First 1).Source
$script:LogPath = Join-Path $script:DataDirectory "worker.log"
$script:Arguments = '-X utf8 -u -m jarvis worker --log "{0}"' -f $script:LogPath
$script:MockTask = $null
$script:BackupTask = $null
$script:StartCount = 0

function Add-TestTrace {
    param([string]$Message)
    Add-Content -LiteralPath $script:TracePath -Value $Message -Encoding ASCII
}

function New-MockTask {
    param([string]$Description, [string]$State = "Ready")
    $logon = [pscustomobject]@{ UserId = $script:CurrentUser; Repetition = $null; Kind = "Logon" }
    $watchdog = [pscustomobject]@{
        UserId = $null
        Repetition = [pscustomobject]@{ Interval = "00:15:00" }
        Kind = "Watchdog"
    }
    return [pscustomobject]@{
        TaskName = $script:TaskName
        TaskPath = "\"
        Description = $Description
        Principal = [pscustomobject]@{ UserId = $script:CurrentUser; LogonType = "Interactive"; RunLevel = "Limited" }
        Actions = @([pscustomobject]@{ Execute = $script:Python; Arguments = $script:Arguments; WorkingDirectory = $script:Project })
        Triggers = @($logon, $watchdog)
        Settings = [pscustomobject]@{
            ExecutionTimeLimit = [TimeSpan]::Zero
            RestartCount = 255
            RestartInterval = [TimeSpan]::FromMinutes(1)
            MultipleInstances = "IgnoreNew"
            StartWhenAvailable = $true
            AllowStartIfOnBatteries = $true
            DontStopIfGoingOnBatteries = $true
            WakeToRun = $true
        }
        State = $State
    }
}

if ($script:Scenario -in @(
    "owned_running_no_heartbeat", "foreign_install", "owned_running_uninstall",
    "foreign_uninstall", "unregister_sticky"
)) {
    $ownedDescription = if ($script:Scenario -like "foreign_*") { "FOREIGN TASK" } else { $script:Description }
    $initialState = if ($script:Scenario -like "*running*") { "Running" } else { "Ready" }
    $script:MockTask = New-MockTask -Description $ownedDescription -State $initialState
    $script:BackupTask = New-MockTask -Description $ownedDescription -State "Ready"
}

function Get-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath)
    if ($script:Scenario -eq "access_denied_uninstall") {
        throw [UnauthorizedAccessException]::new("simulated scheduler access denial")
    }
    if ($null -eq $script:MockTask) {
        $exception = [System.Management.Automation.ItemNotFoundException]::new("mock task not found")
        $record = [System.Management.Automation.ErrorRecord]::new(
            $exception, "TaskNotFound", [System.Management.Automation.ErrorCategory]::ObjectNotFound, $TaskName
        )
        $PSCmdlet.ThrowTerminatingError($record)
    }
    return $script:MockTask
}

function New-ScheduledTaskAction {
    [CmdletBinding()]
    param([string]$Execute, [string]$Argument, [string]$WorkingDirectory)
    return [pscustomobject]@{ Execute = $Execute; Arguments = $Argument; WorkingDirectory = $WorkingDirectory }
}

function New-ScheduledTaskTrigger {
    [CmdletBinding()]
    param(
        [switch]$AtLogOn, [string]$User, [switch]$Once,
        [DateTime]$At, [TimeSpan]$RepetitionInterval
    )
    if ($AtLogOn) {
        return [pscustomobject]@{ UserId = $User; Repetition = $null; Kind = "Logon" }
    }
    return [pscustomobject]@{
        UserId = $null
        Repetition = [pscustomobject]@{ Interval = $RepetitionInterval.ToString() }
        Kind = "Watchdog"
    }
}

function New-ScheduledTaskSettingsSet {
    [CmdletBinding()]
    param(
        [TimeSpan]$ExecutionTimeLimit, [int]$RestartCount, [TimeSpan]$RestartInterval,
        [string]$MultipleInstances, [switch]$StartWhenAvailable,
        [switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries,
        [switch]$WakeToRun
    )
    return [pscustomobject]@{
        ExecutionTimeLimit = $ExecutionTimeLimit
        RestartCount = $RestartCount
        RestartInterval = $RestartInterval
        MultipleInstances = $MultipleInstances
        StartWhenAvailable = $StartWhenAvailable.IsPresent
        AllowStartIfOnBatteries = $AllowStartIfOnBatteries.IsPresent
        DontStopIfGoingOnBatteries = $DontStopIfGoingOnBatteries.IsPresent
        WakeToRun = $WakeToRun.IsPresent
    }
}

function New-ScheduledTaskPrincipal {
    [CmdletBinding()]
    param([string]$UserId, [string]$LogonType, [string]$RunLevel)
    return [pscustomobject]@{ UserId = $UserId; LogonType = $LogonType; RunLevel = $RunLevel }
}

function Export-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath)
    Add-TestTrace "EXPORT"
    return "<Task />"
}

function Register-ScheduledTask {
    [CmdletBinding()]
    param(
        [string]$TaskName, [string]$TaskPath, $Action, [object[]]$Trigger,
        $Settings, $Principal, [string]$Description, [switch]$Force, [string]$Xml
    )
    if ($Xml) {
        Add-TestTrace "REGISTER_XML"
        $script:MockTask = $script:BackupTask
        return $script:MockTask
    }
    $script:MockTask = [pscustomobject]@{
        TaskName = $TaskName
        TaskPath = $TaskPath
        Description = $Description
        Principal = $Principal
        Actions = @($Action)
        Triggers = @($Trigger)
        Settings = $Settings
        State = "Ready"
    }
    Add-TestTrace "REGISTER_NEW"
    if ($script:Scenario -eq "register_fail_partial") {
        throw "simulated partial registration failure"
    }
    return $script:MockTask
}

function Stop-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath)
    Add-TestTrace "STOP"
    $script:MockTask.State = "Ready"
}

function Start-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath)
    $script:StartCount++
    Add-TestTrace "START"
    if ($script:Scenario -eq "start_fail" -and $script:StartCount -eq 1) {
        throw "simulated start failure"
    }
    $script:MockTask.State = "Running"
    if ($script:Scenario -notlike "*no_heartbeat*") {
        $dataDirectory = $script:DataDirectory
        New-Item -ItemType Directory -Force -Path $dataDirectory | Out-Null
        $timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds().ToString([Globalization.CultureInfo]::InvariantCulture)
        Set-Content -LiteralPath (Join-Path $dataDirectory "worker.heartbeat") -Value "$timestamp 4242 worker:4242:test" -Encoding ASCII
    }
}

function Get-ScheduledTaskInfo {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath)
    return [pscustomobject]@{ LastTaskResult = 1 }
}

function Unregister-ScheduledTask {
    [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "Low")]
    param([string]$TaskName, [string]$TaskPath)
    Add-TestTrace "UNREGISTER"
    if ($script:Scenario -ne "unregister_sticky") {
        $script:MockTask = $null
    }
}

function Start-Sleep {
    [CmdletBinding()]
    param([int]$Seconds, [int]$Milliseconds)
}

$exitCode = 0
try {
    . $script:TaskScript
} catch {
    Add-TestTrace ("ERROR " + $_.Exception.Message)
    [Console]::Error.WriteLine($_.Exception.Message)
    $exitCode = 17
}

if ($null -eq $script:MockTask) {
    $result = [ordered]@{ Exists = $false }
} else {
    $action = @($script:MockTask.Actions)[0]
    $triggers = @($script:MockTask.Triggers)
    $result = [ordered]@{
        Exists = $true
        TaskName = $script:MockTask.TaskName
        TaskPath = $script:MockTask.TaskPath
        Description = $script:MockTask.Description
        State = $script:MockTask.State
        ActionExecute = $action.Execute
        ActionArguments = $action.Arguments
        WorkingDirectory = $action.WorkingDirectory
        PrincipalUser = $script:MockTask.Principal.UserId
        PrincipalLogonType = $script:MockTask.Principal.LogonType
        PrincipalRunLevel = $script:MockTask.Principal.RunLevel
        TriggerCount = $triggers.Count
        RestartCount = $script:MockTask.Settings.RestartCount
        MultipleInstances = $script:MockTask.Settings.MultipleInstances
        ExecutionTimeLimit = $script:MockTask.Settings.ExecutionTimeLimit.ToString()
        StartWhenAvailable = $script:MockTask.Settings.StartWhenAvailable
        AllowStartIfOnBatteries = $script:MockTask.Settings.AllowStartIfOnBatteries
        DontStopIfGoingOnBatteries = $script:MockTask.Settings.DontStopIfGoingOnBatteries
        WakeToRun = $script:MockTask.Settings.WakeToRun
    }
}
$result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $script:ResultPath -Encoding UTF8
exit $exitCode
"""


PRESENCE_HARNESS = r"""
Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$script:Scenario = $env:JARVIS_TEST_SCENARIO
$script:TracePath = $env:JARVIS_TEST_TRACE
$script:Project = [IO.Path]::GetFullPath($env:JARVIS_TEST_PROJECT)
$script:TaskScript = $env:JARVIS_TEST_SCRIPT
$script:LifecycleAction = $env:JARVIS_TEST_ACTION
$script:ResultPath = $env:JARVIS_TEST_RESULT
$script:Pythonw = [IO.Path]::GetFullPath($env:JARVIS_TEST_PYTHONW)
$script:Version = $env:JARVIS_TEST_VERSION
$script:InstallationId = $env:JARVIS_TEST_INSTALLATION_ID
$script:SourceRoot = [IO.Path]::GetFullPath($env:JARVIS_TEST_SOURCE_ROOT)
$script:StateFile = [IO.Path]::GetFullPath($env:JARVIS_TEST_STATE_FILE)
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$script:CurrentUser = $identity.Name
$script:CurrentSid = $identity.User.Value
$script:SafeSid = $script:CurrentSid -replace '[^A-Za-z0-9-]', '-'
$script:TaskName = "JarvisLocalPresence-$script:SafeSid"
$script:Description = "JARVIS_LOCAL_PRESENCE|SID=$script:CurrentSid|ROOT=$script:Project"
$script:Arguments = '-X utf8 -m jarvis presence --no-browser'
$script:MockTask = $null
$script:BackupTask = $null
$script:StartCount = 0
$script:HealthOnline = $false
$script:HealthUptime = 0.0
$script:RuntimeEpoch = "b" * 32
$script:LaunchMode = "direct"
$script:StopProcessCount = 0
$script:StartProcessCount = 0

function Add-TestTrace {
    param([string]$Message)
    Add-Content -LiteralPath $script:TracePath -Value $Message -Encoding ASCII
}

function New-MockPresenceTask {
    param([string]$Description, [string]$State = "Ready")
    return [pscustomobject]@{
        TaskName = $script:TaskName
        TaskPath = "\"
        Description = $Description
        Principal = [pscustomobject]@{
            UserId = $script:CurrentSid
            LogonType = "Interactive"
            RunLevel = "Limited"
        }
        Actions = @([pscustomobject]@{
            Execute = $script:Pythonw
            Arguments = $script:Arguments
            WorkingDirectory = $script:Project
        })
        Triggers = @([pscustomobject]@{ UserId = $script:CurrentUser; Kind = "Logon" })
        Settings = [pscustomobject]@{
            ExecutionTimeLimit = [TimeSpan]::Zero
            RestartCount = 255
            RestartInterval = [TimeSpan]::FromMinutes(1)
            MultipleInstances = "IgnoreNew"
            StartWhenAvailable = $true
            AllowStartIfOnBatteries = $true
            DontStopIfGoingOnBatteries = $true
        }
        State = $State
    }
}

if ($script:Scenario -in @("owned_running", "owned_no_health", "stale_endpoint", "foreign")) {
    $description = if ($script:Scenario -eq "foreign") { "FOREIGN TASK" } else { $script:Description }
    $script:MockTask = New-MockPresenceTask -Description $description -State "Running"
    $script:BackupTask = New-MockPresenceTask -Description $description -State "Ready"
    $script:HealthOnline = $true
    $script:HealthUptime = 3600.0
    $script:RuntimeEpoch = "a" * 32
}
if ($script:Scenario -eq "manual") {
    $script:HealthOnline = $true
    $script:HealthUptime = 30.0
    $script:RuntimeEpoch = "a" * 32
    $script:LaunchMode = "manual"
}
if ($script:Scenario -eq "stale_install") {
    $script:HealthOnline = $true
    $script:HealthUptime = 30.0
    $script:RuntimeEpoch = "a" * 32
    $script:LaunchMode = "manual"
}

function Get-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath)
    if ($null -eq $script:MockTask) {
        $exception = [System.Management.Automation.ItemNotFoundException]::new("mock task not found")
        $record = [System.Management.Automation.ErrorRecord]::new(
            $exception, "TaskNotFound", [System.Management.Automation.ErrorCategory]::ObjectNotFound, $TaskName
        )
        $PSCmdlet.ThrowTerminatingError($record)
    }
    return $script:MockTask
}

function New-ScheduledTaskAction {
    [CmdletBinding()]
    param([string]$Execute, [string]$Argument, [string]$WorkingDirectory)
    return [pscustomobject]@{ Execute = $Execute; Arguments = $Argument; WorkingDirectory = $WorkingDirectory }
}

function New-ScheduledTaskTrigger {
    [CmdletBinding()]
    param([switch]$AtLogOn, [string]$User)
    return [pscustomobject]@{ UserId = $User; Kind = "Logon" }
}

function New-ScheduledTaskPrincipal {
    [CmdletBinding()]
    param([string]$UserId, [string]$LogonType, [string]$RunLevel)
    return [pscustomobject]@{ UserId = $UserId; LogonType = $LogonType; RunLevel = $RunLevel }
}

function New-ScheduledTaskSettingsSet {
    [CmdletBinding()]
    param(
        [TimeSpan]$ExecutionTimeLimit, [int]$RestartCount, [TimeSpan]$RestartInterval,
        [string]$MultipleInstances, [switch]$StartWhenAvailable,
        [switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries
    )
    return [pscustomobject]@{
        ExecutionTimeLimit = $ExecutionTimeLimit
        RestartCount = $RestartCount
        RestartInterval = $RestartInterval
        MultipleInstances = $MultipleInstances
        StartWhenAvailable = $StartWhenAvailable.IsPresent
        AllowStartIfOnBatteries = $AllowStartIfOnBatteries.IsPresent
        DontStopIfGoingOnBatteries = $DontStopIfGoingOnBatteries.IsPresent
    }
}

function Export-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath)
    Add-TestTrace "EXPORT"
    return "<Task />"
}

function Register-ScheduledTask {
    [CmdletBinding()]
    param(
        [string]$TaskName, [string]$TaskPath, [string]$Description,
        $Action, $Trigger, $Principal, $Settings, [switch]$Force, [string]$Xml
    )
    if ($Xml) {
        Add-TestTrace "REGISTER_XML"
        $script:MockTask = $script:BackupTask
        return $script:MockTask
    }
    $script:MockTask = [pscustomobject]@{
        TaskName = $TaskName
        TaskPath = $TaskPath
        Description = $Description
        Principal = $Principal
        Actions = @($Action)
        Triggers = @($Trigger)
        Settings = $Settings
        State = "Ready"
    }
    Add-TestTrace "REGISTER_NEW"
    return $script:MockTask
}

function Stop-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath)
    Add-TestTrace "STOP"
    $script:MockTask.State = "Ready"
    if ($script:Scenario -ne "stale_endpoint") {
        $script:HealthOnline = $false
    }
}

function Start-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath)
    $script:StartCount++
    Add-TestTrace "START"
    $script:MockTask.State = "Running"
    if ($script:Scenario -eq "new_no_health") {
        $script:HealthOnline = $false
    } elseif ($script:Scenario -eq "owned_no_health" -and $script:StartCount -eq 1) {
        $script:HealthOnline = $false
    } else {
        $script:HealthOnline = $true
        $script:HealthUptime = 0.1
        $script:RuntimeEpoch = "b" * 32
        $script:LaunchMode = "direct"
    }
}

function Get-Process {
    [CmdletBinding()]
    param([int]$Id)
    if ($Id -ne 4242) { throw "mock process not found" }
    return [pscustomobject]@{
        Id = 4242
        Path = $script:Pythonw
        StartTime = [DateTime]::Parse("2026-08-28T12:00:00Z")
        HasExited = $false
    }
}

function Stop-Process {
    [CmdletBinding()]
    param([int]$Id, [switch]$Force)
    if ($Id -ne 4242) { throw "wrong process" }
    $script:StopProcessCount++
    $script:HealthOnline = $false
    Add-TestTrace "STOP_PROCESS $Id"
}

function Start-Process {
    [CmdletBinding()]
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory,
        [string]$WindowStyle,
        [switch]$PassThru
    )
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals(
        [IO.Path]::GetFullPath($FilePath), $script:Pythonw
    )) {
        Add-TestTrace "OPEN $FilePath"
        return
    }
    $script:StartProcessCount++
    $script:HealthOnline = $true
    $script:HealthUptime = 0.1
    $script:RuntimeEpoch = "b" * 32
    $script:LaunchMode = "manual"
    Add-TestTrace "START_PROCESS 4242"
    return [pscustomobject]@{
        Id = 4242
        Path = $script:Pythonw
        StartTime = [DateTime]::Parse("2026-08-28T12:00:00Z")
        HasExited = $false
    }
}

function Unregister-ScheduledTask {
    [CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "Low")]
    param([string]$TaskName, [string]$TaskPath)
    Add-TestTrace "UNREGISTER"
    $script:MockTask = $null
    $script:HealthOnline = $false
}

function Invoke-RestMethod {
    param([string]$Uri, [string]$Method, [int]$TimeoutSec, [string]$ErrorAction)
    Add-TestTrace "HEALTH $Uri"
    if (-not $script:HealthOnline) {
        throw "mock endpoint offline"
    }
    return [pscustomobject]@{
        service = "jarvis-presence"
        ready = $true
        uptime_seconds = $script:HealthUptime
        version = $script:Version
        installation_id = if ($script:Scenario -eq "stale_install") { "e" * 64 } else { $script:InstallationId }
        source_root = $script:SourceRoot
        python_executable = $script:Pythonw
        process_id = 4242
        runtime_epoch = $script:RuntimeEpoch
        launch_mode = $script:LaunchMode
    }
}

function Start-Sleep {
    [CmdletBinding()]
    param([int]$Seconds, [int]$Milliseconds)
}

$exitCode = 0
try {
    if ($script:LifecycleAction) {
        . $script:TaskScript -Action $script:LifecycleAction -NoBrowser
    } else {
        . $script:TaskScript
    }
} catch {
    Add-TestTrace ("ERROR " + $_.Exception.Message)
    [Console]::Error.WriteLine($_.Exception.Message)
    $exitCode = 17
}

if ($null -eq $script:MockTask) {
    $result = [ordered]@{
        Exists = $false
        HealthOnline = $script:HealthOnline
        StopProcessCount = $script:StopProcessCount
        StartProcessCount = $script:StartProcessCount
    }
} else {
    $taskAction = @($script:MockTask.Actions)[0]
    $result = [ordered]@{
        Exists = $true
        State = $script:MockTask.State
        Description = $script:MockTask.Description
        ActionExecute = $taskAction.Execute
        ActionArguments = $taskAction.Arguments
        WorkingDirectory = $taskAction.WorkingDirectory
        MultipleInstances = $script:MockTask.Settings.MultipleInstances
        HealthOnline = $script:HealthOnline
        StopProcessCount = $script:StopProcessCount
        StartProcessCount = $script:StartProcessCount
    }
}
$result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $script:ResultPath -Encoding UTF8
exit $exitCode
"""


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell is required")
class WindowsScriptTests(unittest.TestCase):
    def setUp(self):
        TEST_TEMP_ROOT.mkdir(exist_ok=True)
        safe_name = f"win space \u00fc {self._testMethodName}"
        self.temp_path = (TEST_TEMP_ROOT.resolve() / safe_name).resolve()
        if self.temp_path.parent != TEST_TEMP_ROOT.resolve():
            raise RuntimeError("Temporary test path escaped its scoped root")
        if self.temp_path.exists():
            shutil.rmtree(self.temp_path)
        self.temp_path.mkdir()
        self.addCleanup(self._cleanup)
        self.project = self.temp_path / "JARVIS project \u00fc with spaces"
        self.project.mkdir()
        self.fake_bin = self.temp_path / "fake bin"
        self.fake_bin.mkdir()
        self.trace = self.temp_path / "native-trace.txt"

    def test_presence_identity_treats_legacy_minimal_health_as_stale(self):
        lifecycle = ROOT / "presence_lifecycle.ps1"
        command = "\n".join((
            '$ErrorActionPreference = "Stop"',
            f'. "{lifecycle}"',
            '$runtime = [pscustomobject]@{',
            '  Version = "0.6.2";',
            f'  InstallationId = "{"d" * 64}";',
            f'  SourceRoot = "{self.project}";',
            f'  Pythonw = "{self.fake_bin / "pythonw.exe"}"',
            '}',
            '$health = [pscustomobject]@{',
            '  service = "jarvis-presence";',
            '  ready = $true;',
            f'  runtime_epoch = "{"e" * 32}"',
            '}',
            'if (Test-PresenceHealthIdentity -Health $health -Runtime $runtime) { exit 9 }',
            'Write-Output "STALE"',
        ))
        completed = subprocess.run(
            [
                str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", command,
            ],
            cwd=self.project,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("STALE", completed.stdout)

    def _cleanup(self) -> None:
        target = self.temp_path.resolve()
        if target.parent != TEST_TEMP_ROOT.resolve():
            raise RuntimeError("Refusing to clean a path outside the test root")
        if target.exists():
            shutil.rmtree(target)
        try:
            TEST_TEMP_ROOT.rmdir()
        except OSError:
            pass

    @staticmethod
    def _write_cmd(path: Path, lines: list[str]) -> None:
        path.write_bytes(("\r\n".join(lines) + "\r\n").encode("ascii"))

    def _base_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PATH"] = str(self.fake_bin) + os.pathsep + env.get("PATH", "")
        env["JARVIS_TEST_TRACE"] = str(self.trace)
        return env

    def _setup_fakes(
        self,
        required: tuple[str, ...],
        installed_before: tuple[str, ...],
        *,
        installed_after: tuple[str, ...] | None = None,
        python_version: str = "3.13.0",
        inventory_exit: int = 0,
        pull_exit: int = 0,
        canary_exit: int = 0,
        doctor_exit: int = 0,
        before_text: str | None = None,
        ollama_enabled: bool | None = None,
    ) -> dict[str, str]:
        before = self.temp_path / "inventory-before.txt"
        after = self.temp_path / "inventory-after.txt"
        seen = self.temp_path / "inventory-seen.txt"
        if before_text is None:
            before_payload = {"required": required, "installed": installed_before}
            if ollama_enabled is not None:
                before_payload["ollama_enabled"] = ollama_enabled
            before_text = "JARVIS_MODEL_INVENTORY=" + json.dumps(
                before_payload, ensure_ascii=True
            )
        after_models = installed_before if installed_after is None else installed_after
        after_payload = {"required": required, "installed": after_models}
        if ollama_enabled is not None:
            after_payload["ollama_enabled"] = ollama_enabled
        after_text = "JARVIS_MODEL_INVENTORY=" + json.dumps(
            after_payload, ensure_ascii=True
        )
        before.write_text(before_text + "\n", encoding="ascii")
        after.write_text(after_text + "\n", encoding="ascii")
        self._write_cmd(
            self.fake_bin / "ollama.cmd",
            [
                "@echo off",
                'if /I "%~1"=="pull" (',
                '  >>"%JARVIS_TEST_TRACE%" echo ollama pull %~2',
                f"  exit /b {pull_exit}",
                ")",
                'if /I "%~1"=="list" >>"%JARVIS_TEST_TRACE%" echo FORBIDDEN ollama list',
                "exit /b 64",
            ],
        )
        self._write_cmd(
            self.fake_bin / "python.cmd",
            [
                "@echo off",
                'if "%~1"=="-c" (',
                f"  echo {python_version}",
                "  exit /b 0",
                ")",
                'if "%~3"=="-c" (',
                '  >>"%JARVIS_TEST_TRACE%" echo python inventory',
                '  if exist "%JARVIS_TEST_INVENTORY_SEEN%" (',
                '    type "%JARVIS_TEST_INVENTORY_AFTER%"',
                "  ) else (",
                '    >"%JARVIS_TEST_INVENTORY_SEEN%" echo seen',
                '    type "%JARVIS_TEST_INVENTORY_BEFORE%"',
                "  )",
                f"  exit /b {inventory_exit}",
                ")",
                'if "%~4"=="jarvis.provider_setup" if "%~5"=="--canary" (',
                '  >>"%JARVIS_TEST_TRACE%" echo PROVIDER_CANARY',
                f"  exit /b {canary_exit}",
                ")",
                '>>"%JARVIS_TEST_TRACE%" echo python %*',
                f"exit /b {doctor_exit}",
            ],
        )
        env = self._base_env()
        env["JARVIS_TEST_INVENTORY_BEFORE"] = str(before)
        env["JARVIS_TEST_INVENTORY_AFTER"] = str(after)
        env["JARVIS_TEST_INVENTORY_SEEN"] = str(seen)
        return env

    def _run_setup(self, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        shutil.copy2(ROOT / "setup.ps1", self.project / "setup.ps1")
        return subprocess.run(
            [str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.project / "setup.ps1")],
            cwd=self.project,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )

    def _scheduler_fakes(self, *, doctor_exit: int = 0) -> dict[str, str]:
        self._write_cmd(
            self.fake_bin / "python.cmd",
            [
                "@echo off",
                'if "%~1"=="-c" ( echo 3.13.0 & exit /b 0 )',
                'if "%~3"=="-c" ( echo JARVIS_RUNTIME_CONFIG=%JARVIS_TEST_DATA_JSON% & exit /b 0 )',
                'if "%~4"=="jarvis.provider_setup" (',
                '  >>"%JARVIS_TEST_TRACE%" echo PROVIDER_SETUP',
                "  exit /b 0",
                ")",
                '>>"%JARVIS_TEST_TRACE%" echo python %*',
                f"exit /b {doctor_exit}",
            ],
        )
        return self._base_env()

    def _run_lifecycle(
        self,
        script_name: str,
        scenario: str,
        data_directory: Path | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict]:
        target_script = self.project / script_name
        shutil.copy2(ROOT / script_name, target_script)
        harness = self.temp_path / "scheduler-harness.ps1"
        harness.write_text(SCHEDULER_HARNESS, encoding="utf-8-sig")
        result_path = self.temp_path / "scheduler-result.json"
        data_directory = data_directory or (self.project / "data")
        env = self._scheduler_fakes()
        env.update(
            {
                "JARVIS_TEST_SCENARIO": scenario,
                "JARVIS_TEST_PROJECT": str(self.project),
                "JARVIS_TEST_SCRIPT": str(target_script),
                "JARVIS_TEST_RESULT": str(result_path),
                "JARVIS_TEST_DATA": str(data_directory),
                "JARVIS_TEST_DATA_JSON": json.dumps({"data_dir": str(data_directory)}),
            }
        )
        completed = subprocess.run(
            [str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
            cwd=self.project,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            check=False,
        )
        if not result_path.exists():
            self.fail(
                "Scheduler harness did not write a result. "
                f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
            )
        result = json.loads(result_path.read_text(encoding="utf-8-sig"))
        return completed, result

    def _run_presence_install(
        self,
        scenario: str,
        *,
        port: int = 9988,
        script_name: str = "install_presence.ps1",
    ) -> tuple[subprocess.CompletedProcess[str], dict]:
        target_script = self.project / script_name
        shutil.copy2(ROOT / script_name, target_script)
        shutil.copy2(ROOT / "presence_lifecycle.ps1", self.project / "presence_lifecycle.ps1")
        harness = self.temp_path / "presence-harness.ps1"
        harness.write_text(PRESENCE_HARNESS, encoding="utf-8-sig")
        result_path = self.temp_path / "presence-result.json"
        pythonw = self.fake_bin / "pythonw.exe"
        pythonw.write_bytes(b"MZ")
        config_path = self.temp_path / "presence-config.txt"
        config_path.write_text(
            "JARVIS_PRESENCE_CONFIG="
            + json.dumps(
                {
                    "port": port,
                    "pythonw": str(pythonw),
                    "source_root": str(self.project),
                    "version": "0.6.2",
                    "installation_id": "d" * 64,
                    "state_file": str(self.temp_path / "presence-manual.json"),
                },
                ensure_ascii=True,
            )
            + "\n",
            encoding="ascii",
        )
        if scenario == "manual_unhealthy":
            (self.temp_path / "presence-manual.json").write_text(
                json.dumps(
                    {
                        "installation_id": "d" * 64,
                        "version": "0.6.2",
                        "source_root": str(self.project),
                        "python_executable": str(pythonw),
                        "process_id": 4242,
                        "process_started_at": "2026-08-28T12:00:00.0000000Z",
                        "runtime_epoch": "a" * 32,
                        "port": port,
                    }
                ),
                encoding="utf-8",
            )
        self._write_cmd(
            self.fake_bin / "python.cmd",
            [
                "@echo off",
                'if "%~4"=="jarvis.provider_setup" (',
                '  >>"%JARVIS_TEST_TRACE%" echo PROVIDER_SETUP',
                "  exit /b 0",
                ")",
                'if "%~3"=="-c" (',
                '  >>"%JARVIS_TEST_TRACE%" echo RUNTIME_CONFIG',
                '  type "%JARVIS_TEST_PRESENCE_CONFIG%"',
                "  exit /b 0",
                ")",
                "exit /b 64",
            ],
        )
        env = self._base_env()
        env.update(
            {
                "JARVIS_TEST_SCENARIO": scenario,
                "JARVIS_TEST_PROJECT": str(self.project),
                "JARVIS_TEST_SCRIPT": str(target_script),
                "JARVIS_TEST_RESULT": str(result_path),
                "JARVIS_TEST_PYTHONW": str(pythonw),
                "JARVIS_TEST_PRESENCE_CONFIG": str(config_path),
                "JARVIS_TEST_VERSION": "0.6.2",
                "JARVIS_TEST_INSTALLATION_ID": "d" * 64,
                "JARVIS_TEST_SOURCE_ROOT": str(self.project),
                "JARVIS_TEST_STATE_FILE": str(self.temp_path / "presence-manual.json"),
            }
        )
        completed = subprocess.run(
            [
                str(POWERSHELL),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(harness),
            ],
            cwd=self.project,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        if not result_path.exists():
            self.fail(
                "Presence install harness did not write a result. "
                f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
            )
        result = json.loads(result_path.read_text(encoding="utf-8-sig"))
        return completed, result

    def _run_presence_launcher(
        self,
        scenario: str,
        action: str,
        *,
        port: int = 9988,
    ) -> tuple[subprocess.CompletedProcess[str], dict]:
        target_script = self.project / "start_jarvis_presence.ps1"
        shutil.copy2(ROOT / "start_jarvis_presence.ps1", target_script)
        shutil.copy2(ROOT / "presence_lifecycle.ps1", self.project / "presence_lifecycle.ps1")
        harness = self.temp_path / "presence-harness.ps1"
        harness.write_text(PRESENCE_HARNESS, encoding="utf-8-sig")
        result_path = self.temp_path / "presence-result.json"
        pythonw = self.fake_bin / "pythonw.exe"
        pythonw.write_bytes(b"MZ")
        config_path = self.temp_path / "presence-config.txt"
        config_path.write_text(
            "JARVIS_PRESENCE_CONFIG="
            + json.dumps(
                {
                    "port": port,
                    "pythonw": str(pythonw),
                    "source_root": str(self.project),
                    "version": "0.6.2",
                    "installation_id": "d" * 64,
                    "state_file": str(self.temp_path / "presence-manual.json"),
                },
                ensure_ascii=True,
            )
            + "\n",
            encoding="ascii",
        )
        if scenario == "manual_unhealthy":
            (self.temp_path / "presence-manual.json").write_text(
                json.dumps(
                    {
                        "installation_id": "d" * 64,
                        "version": "0.6.2",
                        "source_root": str(self.project),
                        "python_executable": str(pythonw),
                        "process_id": 4242,
                        "process_started_at": "2026-08-28T12:00:00.0000000Z",
                        "runtime_epoch": "a" * 32,
                        "port": port,
                    }
                ),
                encoding="utf-8",
            )
        self._write_cmd(
            self.fake_bin / "python.cmd",
            [
                "@echo off",
                'if "%~4"=="jarvis.provider_setup" ( exit /b 0 )',
                'if "%~3"=="-c" ( type "%JARVIS_TEST_PRESENCE_CONFIG%" & exit /b 0 )',
                "exit /b 64",
            ],
        )
        env = self._base_env()
        env.update(
            {
                "JARVIS_TEST_SCENARIO": scenario,
                "JARVIS_TEST_ACTION": action,
                "JARVIS_TEST_PROJECT": str(self.project),
                "JARVIS_TEST_SCRIPT": str(target_script),
                "JARVIS_TEST_RESULT": str(result_path),
                "JARVIS_TEST_PYTHONW": str(pythonw),
                "JARVIS_TEST_PRESENCE_CONFIG": str(config_path),
                "JARVIS_TEST_VERSION": "0.6.2",
                "JARVIS_TEST_INSTALLATION_ID": "d" * 64,
                "JARVIS_TEST_SOURCE_ROOT": str(self.project),
                "JARVIS_TEST_STATE_FILE": str(self.temp_path / "presence-manual.json"),
            }
        )
        completed = subprocess.run(
            [
                str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", str(harness),
            ],
            cwd=self.project,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        if not result_path.exists():
            self.fail(
                "Presence launcher harness did not write a result. "
                f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
            )
        result = json.loads(result_path.read_text(encoding="utf-8-sig"))
        return completed, result

    def _trace_lines(self) -> list[str]:
        if not self.trace.exists():
            return []
        return self.trace.read_text(encoding="ascii", errors="replace").splitlines()

    def test_setup_uses_config_and_ollama_json_inventory(self):
        required = ("fast-custom:1", "reason-custom:2", "code-custom:3")
        env = self._setup_fakes(required, (required[0],), installed_after=required)
        completed = self._run_setup(env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            [line for line in self._trace_lines() if line.startswith("ollama pull")],
            ["ollama pull reason-custom:2", "ollama pull code-custom:3"],
        )
        self.assertEqual(self._trace_lines().count("python inventory"), 2)
        self.assertEqual(self._trace_lines().count("PROVIDER_CANARY"), 1)
        self.assertNotIn("FORBIDDEN", "\n".join(self._trace_lines()))
        self.assertIn("Ready.", completed.stdout)
        self.assertIn("start_jarvis_presence.bat", completed.stdout)
        self.assertIn("does not create a virtual environment", completed.stdout)

    def test_setup_skips_case_insensitive_exact_installs(self):
        required = ("one:1", "two:2", "three:3")
        env = self._setup_fakes(required, tuple(model.upper() for model in required))
        completed = self._run_setup(env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(any(line.startswith("ollama pull") for line in self._trace_lines()))
        self.assertIn("python -X utf8 -m jarvis doctor", self._trace_lines())

    def test_setup_accepts_latest_tag_for_colonless_model(self):
        required = ("qwen3", "reason:1", "code:1")
        installed = ("qwen3:latest", "reason:1", "code:1")
        env = self._setup_fakes(required, installed)
        completed = self._run_setup(env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(any(line.startswith("ollama pull") for line in self._trace_lines()))

    def test_setup_cloud_only_provider_does_not_require_ollama(self):
        env = self._setup_fakes((), (), ollama_enabled=False)
        (self.fake_bin / "ollama.cmd").unlink()
        env["PATH"] = str(self.fake_bin)
        completed = self._run_setup(env)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("Ollama was not found", completed.stderr)
        self.assertFalse(any(line.startswith("ollama ") for line in self._trace_lines()))

    def test_setup_rejects_old_python_before_inventory(self):
        env = self._setup_fakes(("a", "b", "c"), (), python_version="3.10.9")
        completed = self._run_setup(env)
        self.assertNotEqual(completed.returncode, 0)
        self.assertNotIn("python inventory", self._trace_lines())
        self.assertNotIn("Ready.", completed.stdout)

    def test_setup_fails_on_inventory_error_or_malformed_json(self):
        for inventory_exit, text in ((7, None), (0, "JARVIS_MODEL_INVENTORY={bad")):
            with self.subTest(inventory_exit=inventory_exit, text=text):
                if self.trace.exists():
                    self.trace.unlink()
                env = self._setup_fakes(("a", "b", "c"), (), inventory_exit=inventory_exit, before_text=text)
                completed = self._run_setup(env)
                self.assertNotEqual(completed.returncode, 0)
                self.assertNotIn("Ready.", completed.stdout)

    def test_setup_stops_on_pull_failure_and_rechecks_successful_pulls(self):
        required = ("a:1", "b:2", "c:3")
        env = self._setup_fakes(required, ("a:1",), installed_after=required, pull_exit=8)
        completed = self._run_setup(env)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("exit code 8", completed.stderr)
        self.assertNotIn("python -X utf8 -m jarvis doctor", self._trace_lines())

        self.trace.unlink(missing_ok=True)
        env = self._setup_fakes(required, ("a:1",), installed_after=("a:1",))
        completed = self._run_setup(env)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("did not report", completed.stderr)

    def test_setup_stops_when_doctor_fails(self):
        required = ("a:1", "b:2", "c:3")
        env = self._setup_fakes(required, required, doctor_exit=9)
        completed = self._run_setup(env)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("exit code 9", completed.stderr)
        self.assertNotIn("Ready.", completed.stdout)

    def test_setup_stops_when_first_turn_canary_fails_before_doctor(self):
        required = ("a:1", "b:2", "c:3")
        env = self._setup_fakes(required, required, canary_exit=10)
        completed = self._run_setup(env)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("exit code 10", completed.stderr)
        self.assertIn("PROVIDER_CANARY", self._trace_lines())
        self.assertNotIn("python -X utf8 -m jarvis doctor", self._trace_lines())
        self.assertNotIn("Ready.", completed.stdout)

    def test_install_registers_direct_per_user_reliable_task(self):
        completed, result = self._run_lifecycle("install_worker.ps1", "none")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(result["Exists"])
        self.assertTrue(result["TaskName"].startswith("JarvisLocalWorker-S-1-"))
        self.assertIn("JARVIS_LOCAL_WORKER|SID=S-1-", result["Description"])
        self.assertNotIn("cmd.exe", result["ActionExecute"].lower())
        self.assertEqual(
            result["ActionArguments"],
            f'-X utf8 -u -m jarvis worker --log "{self.project / "data" / "worker.log"}"',
        )
        self.assertEqual(result["WorkingDirectory"].casefold(), str(self.project).casefold())
        self.assertEqual(result["PrincipalLogonType"], "Interactive")
        self.assertEqual(result["PrincipalRunLevel"], "Limited")
        self.assertEqual(result["TriggerCount"], 2)
        self.assertEqual(result["RestartCount"], 255)
        self.assertEqual(result["MultipleInstances"], "IgnoreNew")
        self.assertEqual(result["ExecutionTimeLimit"], "00:00:00")
        for key in ("StartWhenAvailable", "AllowStartIfOnBatteries", "DontStopIfGoingOnBatteries", "WakeToRun"):
            self.assertTrue(result[key], key)
        self.assertIn("only while", completed.stdout)
        trace = self._trace_lines()
        self.assertLess(trace.index("PROVIDER_SETUP"), trace.index("python -X utf8 -m jarvis doctor"))
        self.assertEqual([line for line in self._trace_lines() if line in {"REGISTER_NEW", "START"}], ["REGISTER_NEW", "START"])

    def test_install_rejects_foreign_collision_without_mutation(self):
        completed, result = self._run_lifecycle("install_worker.ps1", "foreign_install")
        self.assertNotEqual(completed.returncode, 0)
        self.assertTrue(result["Exists"])
        trace = self._trace_lines()
        self.assertFalse(any(line in {"EXPORT", "STOP", "REGISTER_NEW", "UNREGISTER"} for line in trace))
        self.assertIn("Refusing to modify", "\n".join(trace))

    def test_worker_install_and_uninstall_honor_custom_data_directory(self):
        custom_data = self.temp_path / "custom worker state"
        completed, result = self._run_lifecycle("install_worker.ps1", "none", custom_data)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        expected_log = custom_data / "worker.log"
        self.assertEqual(
            result["ActionArguments"],
            f'-X utf8 -u -m jarvis worker --log "{expected_log}"',
        )
        self.assertIn(f"|DATA={custom_data}", result["Description"])

        self.trace.unlink(missing_ok=True)
        completed, result = self._run_lifecycle(
            "uninstall_worker.ps1", "owned_running_uninstall", custom_data
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(result["Exists"])

    def test_install_rolls_back_partial_registration_and_start_failure(self):
        for scenario in ("register_fail_partial", "start_fail"):
            with self.subTest(scenario=scenario):
                if self.trace.exists():
                    self.trace.unlink()
                completed, result = self._run_lifecycle("install_worker.ps1", scenario)
                self.assertNotEqual(completed.returncode, 0)
                self.assertFalse(result["Exists"])
                self.assertIn("REGISTER_NEW", self._trace_lines())
                self.assertIn("UNREGISTER", self._trace_lines())

    def test_install_restores_running_backup_when_heartbeat_fails(self):
        completed, result = self._run_lifecycle("install_worker.ps1", "owned_running_no_heartbeat")
        self.assertNotEqual(completed.returncode, 0)
        self.assertTrue(result["Exists"])
        self.assertEqual(result["State"], "Running")
        scheduler = [line for line in self._trace_lines() if line in {"EXPORT", "STOP", "REGISTER_NEW", "START", "REGISTER_XML", "UNREGISTER"}]
        self.assertEqual(scheduler, ["EXPORT", "STOP", "REGISTER_NEW", "START", "STOP", "REGISTER_XML", "START"])
        self.assertIn("fresh startup heartbeat", "\n".join(self._trace_lines()))

    def test_presence_install_uses_configured_port_and_starts_new_task(self):
        completed, result = self._run_presence_install("none", port=9988)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(result["Exists"])
        self.assertEqual(result["State"], "Running")
        self.assertEqual(result["ActionArguments"], "-X utf8 -m jarvis presence --no-browser")
        self.assertEqual(result["MultipleInstances"], "IgnoreNew")
        trace = self._trace_lines()
        self.assertLess(trace.index("PROVIDER_SETUP"), trace.index("RUNTIME_CONFIG"))
        self.assertLess(trace.index("RUNTIME_CONFIG"), trace.index("REGISTER_NEW"))
        self.assertEqual(
            [line for line in trace if line in {"REGISTER_NEW", "START"}],
            ["REGISTER_NEW", "START"],
        )
        health_checks = [line for line in trace if line.startswith("HEALTH ")]
        self.assertTrue(health_checks)
        self.assertTrue(all("127.0.0.1:9988/api/health" in line for line in health_checks))
        self.assertNotIn("127.0.0.1:8787", "\n".join(trace))

    def test_presence_reinstall_stops_old_ignore_new_instance_before_start(self):
        completed, result = self._run_presence_install("owned_running")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(result["State"], "Running")
        lifecycle = [
            line
            for line in self._trace_lines()
            if line in {"EXPORT", "STOP", "REGISTER_NEW", "START", "REGISTER_XML", "UNREGISTER"}
        ]
        self.assertEqual(lifecycle, ["EXPORT", "STOP", "REGISTER_NEW", "START"])

    def test_presence_install_is_owned_fail_closed(self):
        completed, result = self._run_presence_install("foreign")
        self.assertNotEqual(completed.returncode, 0)
        self.assertTrue(result["Exists"])
        trace = self._trace_lines()
        self.assertFalse(
            any(line in {"EXPORT", "STOP", "REGISTER_NEW", "START", "REGISTER_XML", "UNREGISTER"} for line in trace)
        )
        self.assertIn("does not own", "\n".join(trace))

    def test_presence_install_rolls_back_when_fresh_health_is_missing(self):
        completed, result = self._run_presence_install("owned_no_health")
        self.assertNotEqual(completed.returncode, 0)
        self.assertTrue(result["Exists"])
        self.assertEqual(result["State"], "Running")
        lifecycle = [
            line
            for line in self._trace_lines()
            if line in {"EXPORT", "STOP", "REGISTER_NEW", "START", "REGISTER_XML", "UNREGISTER"}
        ]
        self.assertEqual(
            lifecycle,
            ["EXPORT", "STOP", "REGISTER_NEW", "START", "STOP", "REGISTER_XML", "START"],
        )
        self.assertIn("fresh healthy endpoint", "\n".join(self._trace_lines()))

    def test_presence_install_rejects_stale_health_after_task_stop(self):
        completed, result = self._run_presence_install("stale_endpoint")
        self.assertNotEqual(completed.returncode, 0)
        self.assertTrue(result["Exists"])
        self.assertEqual(result["State"], "Running")
        lifecycle = [
            line
            for line in self._trace_lines()
            if line in {"EXPORT", "STOP", "REGISTER_NEW", "START", "REGISTER_XML", "UNREGISTER"}
        ]
        self.assertEqual(lifecycle, ["EXPORT", "STOP", "REGISTER_XML", "START"])
        self.assertIn("remained healthy", "\n".join(self._trace_lines()))

    def test_presence_install_exactly_stops_verified_manual_instance(self):
        completed, result = self._run_presence_install("manual")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(result["Exists"])
        self.assertEqual(result["StopProcessCount"], 1)
        trace = self._trace_lines()
        self.assertLess(trace.index("STOP_PROCESS 4242"), trace.index("REGISTER_NEW"))
        self.assertLess(trace.index("REGISTER_NEW"), trace.index("START"))

    def test_presence_install_rejects_stale_install_without_stopping_it(self):
        completed, result = self._run_presence_install("stale_install")
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(result["Exists"])
        self.assertEqual(result["StopProcessCount"], 0)
        self.assertNotIn("REGISTER_NEW", self._trace_lines())
        self.assertIn("different or stale", completed.stderr)

    def test_presence_uninstall_exactly_stops_verified_manual_instance(self):
        completed, result = self._run_presence_install(
            "manual", script_name="uninstall_presence.ps1"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(result["Exists"])
        self.assertFalse(result["HealthOnline"])
        self.assertEqual(result["StopProcessCount"], 1)

    def test_presence_uninstall_rejects_stale_install_without_stopping_it(self):
        completed, result = self._run_presence_install(
            "stale_install", script_name="uninstall_presence.ps1"
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertTrue(result["HealthOnline"])
        self.assertEqual(result["StopProcessCount"], 0)

    def test_presence_install_takes_over_unhealthy_manual_instance_from_exact_state(self):
        completed, result = self._run_presence_install("manual_unhealthy")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(result["Exists"])
        self.assertEqual(result["StopProcessCount"], 1)
        trace = self._trace_lines()
        self.assertLess(trace.index("STOP_PROCESS 4242"), trace.index("REGISTER_NEW"))

    def test_presence_uninstall_stops_unhealthy_manual_instance_from_exact_state(self):
        completed, result = self._run_presence_install(
            "manual_unhealthy", script_name="uninstall_presence.ps1"
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(result["Exists"])
        self.assertEqual(result["StopProcessCount"], 1)

    def test_presence_launcher_default_start_records_exact_manual_process(self):
        completed, result = self._run_presence_launcher("none", "start")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(result["HealthOnline"])
        self.assertEqual(result["StartProcessCount"], 1)
        self.assertEqual(result["StopProcessCount"], 0)
        self.assertIn("JARVIS Presence is online", completed.stdout)

    def test_presence_launcher_status_reports_exact_runtime_identity(self):
        completed, result = self._run_presence_launcher("manual", "status")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(result["StartProcessCount"], 0)
        self.assertIn("version 0.6.2", completed.stdout)
        self.assertIn("PID 4242", completed.stdout)
        self.assertIn("mode manual", completed.stdout)
        self.assertIn("a" * 32, completed.stdout)

    def test_presence_launcher_stop_targets_only_verified_process(self):
        completed, result = self._run_presence_launcher("manual", "stop")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(result["HealthOnline"])
        self.assertEqual(result["StopProcessCount"], 1)
        self.assertEqual(result["StartProcessCount"], 0)
        self.assertIn("JARVIS Presence stopped", completed.stdout)

    def test_presence_launcher_restart_requires_new_runtime_epoch(self):
        completed, result = self._run_presence_launcher("manual", "restart")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(result["HealthOnline"])
        self.assertEqual(result["StopProcessCount"], 1)
        self.assertEqual(result["StartProcessCount"], 1)
        self.assertIn("b" * 32, completed.stdout)
        trace = self._trace_lines()
        self.assertLess(trace.index("STOP_PROCESS 4242"), trace.index("START_PROCESS 4242"))

    def test_presence_launcher_rejects_stale_install_without_process_action(self):
        completed, result = self._run_presence_launcher("stale_install", "restart")
        self.assertNotEqual(completed.returncode, 0)
        self.assertTrue(result["HealthOnline"])
        self.assertEqual(result["StopProcessCount"], 0)
        self.assertEqual(result["StartProcessCount"], 0)
        self.assertIn("different or stale", completed.stderr)

    def test_presence_launcher_stops_unhealthy_manual_instance_from_exact_state(self):
        completed, result = self._run_presence_launcher("manual_unhealthy", "stop")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(result["StopProcessCount"], 1)
        self.assertEqual(result["StartProcessCount"], 0)
        self.assertIn("JARVIS Presence stopped", completed.stdout)

    def test_presence_launcher_restarts_owned_scheduled_task_without_pid_kill(self):
        completed, result = self._run_presence_launcher("owned_running", "restart")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(result["Exists"])
        self.assertTrue(result["HealthOnline"])
        self.assertEqual(result["StopProcessCount"], 0)
        self.assertEqual(result["StartProcessCount"], 0)
        lifecycle = [line for line in self._trace_lines() if line in {"STOP", "START"}]
        self.assertEqual(lifecycle, ["STOP", "START"])

    def test_uninstall_is_owned_fail_closed_and_verifies_absence(self):
        completed, result = self._run_lifecycle("uninstall_worker.ps1", "owned_running_uninstall")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(result["Exists"])
        self.assertEqual([line for line in self._trace_lines() if line in {"STOP", "UNREGISTER"}], ["STOP", "UNREGISTER"])

        self.trace.unlink(missing_ok=True)
        completed, result = self._run_lifecycle("uninstall_worker.ps1", "unregister_sticky")
        self.assertNotEqual(completed.returncode, 0)
        self.assertTrue(result["Exists"])
        self.assertIn("still reports", "\n".join(self._trace_lines()))

    def test_uninstall_rejects_foreign_and_scheduler_access_errors(self):
        for scenario in ("foreign_uninstall", "access_denied_uninstall"):
            with self.subTest(scenario=scenario):
                if self.trace.exists():
                    self.trace.unlink()
                completed, _ = self._run_lifecycle("uninstall_worker.ps1", scenario)
                self.assertNotEqual(completed.returncode, 0)
                self.assertNotIn("UNREGISTER", self._trace_lines())

    def test_uninstall_missing_task_is_idempotent(self):
        completed, result = self._run_lifecycle("uninstall_worker.ps1", "none")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(result["Exists"])
        self.assertIn("not installed", completed.stdout)

    def test_scripts_parse_in_windows_powershell(self):
        for script_name in (
            "setup.ps1",
            "install_worker.ps1",
            "uninstall_worker.ps1",
            "start_jarvis_presence.ps1",
            "install_presence.ps1",
            "uninstall_presence.ps1",
            "presence_lifecycle.ps1",
        ):
            script = str(ROOT / script_name).replace("'", "''")
            command = (
                "$tokens=$null;$errors=$null;"
                f"[void][System.Management.Automation.Language.Parser]::ParseFile('{script}',[ref]$tokens,[ref]$errors);"
                "if($errors.Count){$errors|ForEach-Object{Write-Error $_};exit 1}"
            )
            completed = subprocess.run(
                [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, f"{script_name}: {completed.stderr}")

    def test_static_release_contracts_and_batch_exit_codes(self):
        setup_source = (ROOT / "setup.ps1").read_text(encoding="utf-8")
        self.assertIn("JARVIS setup stopped safely.", setup_source)
        self.assertIn("python.org/downloads/windows", setup_source)
        self.assertIn("Config.load()", setup_source)
        self.assertIn("OllamaClient", setup_source)
        self.assertIn("client.models(refresh=True)", setup_source)
        self.assertIn("config.model.casefold()", setup_source)
        self.assertIn('$pythonVersion -lt [version]"3.11"', setup_source)
        self.assertIn('$pythonVersion -ge [version]"3.14"', setup_source)
        self.assertIn("Python 3.11, 3.12, or 3.13 is required", setup_source)
        self.assertNotIn("Python 3.11 or newer", setup_source)
        self.assertNotIn('ArgumentList @("list")', setup_source)

        install_source = (ROOT / "install_worker.ps1").read_text(encoding="utf-8")
        for required in (
            "-X utf8 -u -m jarvis worker --log",
            "-LogonType Interactive",
            "-AtLogOn",
            "-RepetitionInterval",
            "-RestartCount 255",
            "-MultipleInstances IgnoreNew",
            "-ExecutionTimeLimit ([TimeSpan]::Zero)",
            "Export-ScheduledTask",
            "Wait-WorkerHeartbeat",
            "Register-ScheduledTask",
        ):
            self.assertIn(required, install_source)
        self.assertNotIn("$env:ComSpec", install_source)

        for batch_name, target in (
            ("setup.bat", "setup.ps1"),
            ("install_worker.bat", "install_worker.ps1"),
            ("uninstall_worker.bat", "uninstall_worker.ps1"),
        ):
            source = (ROOT / batch_name).read_text(encoding="utf-8")
            self.assertIn(target, source)
            self.assertIn('set "JARVIS_EXIT=%ERRORLEVEL%"', source)
            self.assertIn("exit /b %JARVIS_EXIT%", source)

        start_source = (ROOT / "start_jarvis.bat").read_text(encoding="utf-8")
        self.assertIn("python -X utf8 -m jarvis", start_source)
        self.assertIn('set "JARVIS_EXIT=%ERRORLEVEL%"', start_source)
        self.assertIn("exit /b %JARVIS_EXIT%", start_source)

        for batch_name, target in (
            ("start_jarvis_presence.bat", "start_jarvis_presence.ps1"),
            ("install_presence.bat", "install_presence.ps1"),
            ("uninstall_presence.bat", "uninstall_presence.ps1"),
        ):
            source = (ROOT / batch_name).read_text(encoding="utf-8")
            self.assertIn(target, source)
            self.assertIn('set "JARVIS_EXIT=%ERRORLEVEL%"', source)
            self.assertIn("exit /b %JARVIS_EXIT%", source)

        install_presence = (ROOT / "install_presence.ps1").read_text(encoding="utf-8")
        self.assertIn("JARVIS_LOCAL_PRESENCE|SID=", install_presence)
        self.assertIn("-LogonType Interactive", install_presence)
        self.assertIn("-RunLevel Limited", install_presence)
        self.assertIn("-MultipleInstances IgnoreNew", install_presence)
        self.assertIn("presence --no-browser", install_presence)
        self.assertIn("Get-PresenceRuntimeConfig", install_presence)
        self.assertIn("jarvis.provider_setup", install_presence)
        self.assertLess(
            install_presence.index("jarvis.provider_setup"),
            install_presence.index("Get-PresenceRuntimeConfig -PythonPath"),
        )
        self.assertIn("Wait-PresenceOffline", install_presence)
        self.assertIn("Wait-PresenceHealth", install_presence)
        self.assertIn("Export-ScheduledTask", install_presence)
        self.assertIn("Register-ScheduledTask", install_presence)
        self.assertNotIn("127.0.0.1:8787", install_presence)

        install_worker = (ROOT / "install_worker.ps1").read_text(encoding="utf-8")
        self.assertIn("jarvis.provider_setup", install_worker)
        self.assertLess(
            install_worker.index("jarvis.provider_setup"),
            install_worker.index('"jarvis", "doctor"'),
        )

        start_presence = (ROOT / "start_jarvis_presence.ps1").read_text(encoding="utf-8")
        self.assertIn("Get-PresenceRuntimeConfig", start_presence)
        self.assertIn('ValidateSet("start", "status", "restart", "stop")', start_presence)
        self.assertIn("Stop-ExactPresence", start_presence)
        self.assertNotIn("127.0.0.1:8787", start_presence)

        start_presence_batch = (ROOT / "start_jarvis_presence.bat").read_text(
            encoding="utf-8"
        )
        self.assertIn('start_jarvis_presence.ps1" %*', start_presence_batch)

        uninstall_presence = (ROOT / "uninstall_presence.ps1").read_text(encoding="utf-8")
        self.assertIn("Refusing to remove scheduled task", uninstall_presence)
        self.assertIn("Unregister-ScheduledTask", uninstall_presence)

    def test_public_readme_points_nontechnical_users_to_the_supported_first_run(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        guide = (ROOT / "docs" / "WINDOWS_FIRST_RUN.md").read_text(encoding="utf-8")

        self.assertNotIn("Start in two clicks", readme)
        self.assertIn("Exact release source archive", readme)
        self.assertIn("docs/WINDOWS_FIRST_RUN.md", readme)
        self.assertIn("start_jarvis_presence.bat", readme)
        self.assertIn("does not create a", readme)
        self.assertIn("virtual environment", readme)
        self.assertIn("Manual local-only Ollama path", guide)
        self.assertIn("An unchanged copy of `.env.example` does not count", guide)
        self.assertIn("tool-free first-turn", guide)

    @unittest.skipUnless(COMSPEC, "cmd.exe is required")
    def test_start_batch_preserves_python_exit_code(self):
        self._write_cmd(self.fake_bin / "python.cmd", ["@echo off", "exit /b 37"])
        shutil.copy2(ROOT / "start_jarvis.bat", self.project / "start_jarvis.bat")
        completed = subprocess.run(
            [str(COMSPEC), "/d", "/c", "call start_jarvis.bat"],
            cwd=self.project,
            env=self._base_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(completed.returncode, 37)


if __name__ == "__main__":
    unittest.main()

