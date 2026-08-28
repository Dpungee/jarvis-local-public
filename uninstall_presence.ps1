$ErrorActionPreference = "Stop"
$project = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$sid = $identity.User.Value
$safeSid = $sid -replace '[^A-Za-z0-9-]', '-'
$taskName = "JarvisLocalPresence-$safeSid"
$marker = "JARVIS_LOCAL_PRESENCE|SID=$sid|ROOT=$project"
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue

if ($null -eq $existing) {
    Write-Host "Jarvis Presence is not installed as a scheduled task."
    exit 0
}
if ($existing.Description -ne $marker) {
    throw "Refusing to remove scheduled task '$taskName' because Jarvis does not own it."
}

Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
if ($null -ne (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
    throw "The Presence scheduled task still exists after uninstall."
}
Write-Host "Jarvis Presence automatic startup was removed. Data and conversations were preserved."
