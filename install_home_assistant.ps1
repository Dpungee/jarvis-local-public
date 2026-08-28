$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$configPath = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "data\home-assistant"))
$dataRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot "data"))
if (-not $configPath.StartsWith($dataRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Home Assistant configuration path escaped the Jarvis data directory."
}

$serverVersion = docker info --format "{{.ServerVersion}}" 2>$null
if ($LASTEXITCODE -ne 0 -or -not $serverVersion) {
    throw "Docker Desktop is not running. Start it, approve Windows if asked, then retry."
}

New-Item -ItemType Directory -Force -Path $configPath | Out-Null
$existing = docker ps -a --filter "name=^/jarvis-home-assistant$" --format "{{.Names}}"
if ($existing) {
    $image = docker inspect jarvis-home-assistant --format "{{.Config.Image}}"
    if ($image -ne "ghcr.io/home-assistant/home-assistant:stable") {
        throw "The existing jarvis-home-assistant container uses an unexpected image."
    }
    docker start jarvis-home-assistant | Out-Null
} else {
    $mount = $configPath.Replace("\", "/")
    docker run -d `
        --name jarvis-home-assistant `
        --restart unless-stopped `
        --memory 2g `
        --cpus 2 `
        -e TZ=America/New_York `
        -v "${mount}:/config" `
        -p 127.0.0.1:8123:8123 `
        ghcr.io/home-assistant/home-assistant:stable | Out-Null
}

$ready = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    try {
        $response = Invoke-WebRequest `
            -Uri "http://127.0.0.1:8123/" `
            -UseBasicParsing `
            -TimeoutSec 2 `
            -ErrorAction Stop
        if ($response.StatusCode -eq 200) {
            $ready = $true
            break
        }
    } catch {
        Start-Sleep -Seconds 1
    }
}
if (-not $ready) {
    throw "Home Assistant started but did not become ready within 60 seconds."
}

Write-Host "Home Assistant is ready at http://127.0.0.1:8123/"
Start-Process "http://127.0.0.1:8123/"
