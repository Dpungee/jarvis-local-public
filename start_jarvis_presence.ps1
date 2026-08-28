Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$project = [IO.Path]::GetFullPath($PSScriptRoot)

$setupPython = Get-Command -Name "python" -CommandType Application -ErrorAction Stop | Select-Object -First 1
& $setupPython.Source -X utf8 -m jarvis.provider_setup --interactive
$setupExit = $LASTEXITCODE
if ($null -eq $setupExit -or $setupExit -ne 0) {
    exit $(if ($null -eq $setupExit) { 1 } else { $setupExit })
}

function Get-PresenceRuntimeConfig {
    $pythonCommand = Get-Command -Name "python" -CommandType Application -ErrorAction Stop | Select-Object -First 1
    $configCode = @"
import json
import sys
from pathlib import Path
from jarvis.config import Config
config = Config.load()
pythonw = Path(sys.executable).with_name('pythonw.exe')
print('JARVIS_PRESENCE_CONFIG=' + json.dumps({'port': config.presence_port, 'pythonw': str(pythonw.resolve())}, ensure_ascii=True))
"@
    $output = @(& $pythonCommand.Source -X utf8 -c $configCode 2>&1)
    if ($LASTEXITCODE -ne 0) {
        throw "Could not load the Jarvis Presence configuration."
    }
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

$runtime = Get-PresenceRuntimeConfig
$url = "http://127.0.0.1:$($runtime.Port)/"
$health = "${url}api/health"

function Test-PresenceHealth {
    try {
        $response = Invoke-RestMethod -Uri $health -Method Get -TimeoutSec 2 -ErrorAction Stop
        return $response.service -eq "jarvis-presence" -and $response.ready -eq $true
    } catch {
        return $false
    }
}

if (-not (Test-PresenceHealth)) {
    Start-Process `
        -FilePath $runtime.Pythonw `
        -ArgumentList @("-X", "utf8", "-m", "jarvis", "presence", "--no-browser") `
        -WorkingDirectory $project `
        -WindowStyle Hidden

    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $deadline -and -not (Test-PresenceHealth)) {
        Start-Sleep -Milliseconds 250
    }
    if (-not (Test-PresenceHealth)) {
        throw "Jarvis Presence did not become healthy within 30 seconds."
    }
}

Start-Process $url
Write-Host "JARVIS Presence is online at $url"
