param(
    [switch]$NoShortcut
)

# Sets up the JARVIS Agent Hub next to JARVIS: checks that the Hub and its web files load, prepares
# its state folder and adds a "JARVIS Agent Hub" desktop shortcut to start_agent_hub.bat.
# JARVIS_AGENT_HUB_SHORTCUT_DIR overrides the shortcut folder (tests use it).
Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$project = [IO.Path]::GetFullPath($PSScriptRoot)
Set-Location -LiteralPath $project

trap {
    [Console]::Error.WriteLine("")
    [Console]::Error.WriteLine("Agent Hub setup stopped safely.")
    [Console]::Error.WriteLine("$($_.Exception.Message)")
    [Console]::Error.WriteLine("JARVIS itself is installed; rerun install_agent_hub.bat after fixing the item above.")
    exit 1
}

$pythonCommand = Get-Command -Name "python" -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
if (-not $pythonCommand) {
    throw "Python was not found. Run setup.bat first."
}

Write-Host "Checking the Agent Hub..."
& $pythonCommand.Source -X utf8 -c "import jarvis.agent_hub; from importlib.resources import files; assets = files('jarvis').joinpath('agent_hub_static'); missing = [name for name in ('index.html', 'hub.css', 'hub.js') if not assets.joinpath(name).is_file()]; raise SystemExit('Agent Hub web files are missing: ' + ', '.join(missing) if missing else 0)"
$checkExit = $LASTEXITCODE
if ($null -eq $checkExit -or $checkExit -ne 0) {
    throw "The Agent Hub could not be loaded (exit code $checkExit). Rerun setup.bat to reinstall JARVIS."
}
New-Item -ItemType Directory -Force -Path (Join-Path $project "data\agent-hub") | Out-Null

if (-not $NoShortcut) {
    $folder = $env:JARVIS_AGENT_HUB_SHORTCUT_DIR
    if (-not $folder) {
        $folder = [Environment]::GetFolderPath("Desktop")
    }
    if ($folder -and $folder -ne "none") {
        New-Item -ItemType Directory -Force -Path $folder | Out-Null
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut((Join-Path $folder "JARVIS Agent Hub.lnk"))
        $shortcut.TargetPath = Join-Path $project "start_agent_hub.bat"
        $shortcut.WorkingDirectory = $project
        $shortcut.Description = "Open the JARVIS Agent Hub"
        $shortcut.Save()
        Write-Host "Added a 'JARVIS Agent Hub' shortcut to $folder"
    }
}
Write-Host "Agent Hub ready. Double-click start_agent_hub.bat (or the shortcut) to open it."
