param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8790,
    [switch]$NoBrowser
)

# Starts the JARVIS Agent Hub in the background (it keeps running after this window closes) and
# opens it in the browser. State lives in data\agent-hub; the one-time sign-in link, which carries
# the operator token, is kept in data\agent-hub\hub.out and is never printed here.
Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$project = [IO.Path]::GetFullPath($PSScriptRoot)
Set-Location -LiteralPath $project

trap {
    [Console]::Error.WriteLine("")
    [Console]::Error.WriteLine("The Agent Hub did not start.")
    [Console]::Error.WriteLine("$($_.Exception.Message)")
    exit 1
}

$pythonCommand = Get-Command -Name "python" -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $pythonCommand) {
    throw "Python was not found. Run setup.bat first."
}
$stateDir = Join-Path $project "data\agent-hub"
New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
$outLog = Join-Path $stateDir "hub.out"
$errLog = Join-Path $stateDir "hub.err"

function Get-HubLink {
    if (-not (Test-Path -LiteralPath $outLog)) {
        return $null
    }
    $found = Select-String -LiteralPath $outLog -Pattern "JARVIS Agent Hub: (http://127\.0\.0\.1:\d+/#token=[A-Za-z0-9_\-]+)" |
        Select-Object -Last 1
    if ($found) {
        return $found.Matches[0].Groups[1].Value
    }
    return $null
}

function Test-HubPortInUse {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    return [bool]$listener
}

if (-not (Test-HubPortInUse)) {
    Remove-Item -LiteralPath $outLog, $errLog -ErrorAction SilentlyContinue
    Write-Host "Starting the JARVIS Agent Hub..."
    Start-Process -FilePath $pythonCommand.Source `
        -ArgumentList @("-X", "utf8", "-u", "-m", "jarvis.agent_hub", "--port", "$Port") `
        -WorkingDirectory $project -WindowStyle Hidden `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog | Out-Null
    $deadline = (Get-Date).AddSeconds(90)
    while (-not (Get-HubLink)) {
        if ((Get-Date) -gt $deadline) {
            throw "It did not answer within 90 seconds. Details are in $errLog"
        }
        Start-Sleep -Milliseconds 500
    }
}

$link = Get-HubLink
if (-not $link -or $link -notmatch "^http://127\.0\.0\.1:$Port/") {
    throw "Port $Port is already used by another program. Close it, or run start_agent_hub.bat with -Port <free port>."
}
Write-Host "JARVIS Agent Hub is running at http://127.0.0.1:$Port"
if (-not $NoBrowser) {
    Start-Process $link
    Write-Host "Opened it in your browser."
}
