Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

trap {
    [Console]::Error.WriteLine("")
    [Console]::Error.WriteLine("JARVIS setup stopped safely.")
    [Console]::Error.WriteLine("$($_.Exception.Message)")
    [Console]::Error.WriteLine("")
    [Console]::Error.WriteLine("Fix the item above, then double-click setup.bat again.")
    exit 1
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

function Get-JarvisModelInventory {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [string]$PythonPath
    )

    $inventoryCode = @"
import json
from jarvis.config import Config
from jarvis.ollama_client import OllamaClient

config = Config.load()
selected_model = config.model.casefold()
profiles = {'auto', 'fast', 'reasoning', 'coding', 'deep'}
cloud_prefixes = ('openai:', 'anthropic:', 'claude-cli:', 'codex-cli:', 'ollama:')

def local_model(value):
    model = str(value).strip()
    folded = model.casefold()
    if not model or folded in profiles:
        return None
    for prefix in cloud_prefixes:
        if folded.startswith(prefix):
            return model[len(prefix):] if prefix == 'ollama:' else None
    return model

required = []
if config.ollama_enabled:
    required = [
        model for model in (
            local_model(config.fast_model),
            local_model(config.reasoning_model),
            local_model(config.coding_model),
            local_model(config.deep_model),
            local_model(selected_model),
        ) if model
    ]
installed = []
if required:
    client = OllamaClient(
        config.ollama_url,
        allow_remote=config.ollama_allow_remote,
        health_timeout=config.ollama_health_timeout,
        generation_timeout=config.ollama_generation_timeout,
        max_response_bytes=config.ollama_max_response_bytes,
        max_retries=config.ollama_max_retries,
        retry_backoff=config.ollama_retry_backoff,
    )
    installed = client.models(refresh=True)
payload = {
    'ollama_enabled': config.ollama_enabled,
    'required': required,
    'installed': installed,
}
print('JARVIS_MODEL_INVENTORY=' + json.dumps(payload, ensure_ascii=True))
"@

    $output = @(Invoke-NativeCommand -FilePath $PythonPath -ArgumentList @(
        "-X", "utf8", "-c", $inventoryCode
    ) -CaptureOutput)
    $prefix = "JARVIS_MODEL_INVENTORY="
    $line = $output |
        ForEach-Object { "$_".Trim() } |
        Where-Object { $_.StartsWith($prefix, [StringComparison]::Ordinal) } |
        Select-Object -Last 1
    if (-not $line) {
        throw "JARVIS did not return a valid model inventory."
    }

    try {
        $decoded = $line.Substring($prefix.Length) | ConvertFrom-Json -ErrorAction Stop
        $required = @($decoded.required)
        $installed = @($decoded.installed)
        $enabled = if ($decoded.PSObject.Properties.Name -contains "ollama_enabled") {
            [bool]$decoded.ollama_enabled
        } else {
            $true
        }
    } catch {
        throw "JARVIS returned malformed model inventory JSON: $($_.Exception.Message)"
    }

    $uniqueRequired = New-Object 'System.Collections.Generic.List[string]'
    $seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($item in $required) {
        if ($null -eq $item -or -not ($item -is [string]) -or -not $item.Trim()) {
            throw "Every configured Ollama model must be a non-empty string."
        }
        $model = $item.Trim()
        if ($seen.Add($model)) {
            [void]$uniqueRequired.Add($model)
        }
    }
    $cleanInstalled = New-Object 'System.Collections.Generic.List[string]'
    foreach ($item in $installed) {
        if ($item -is [string] -and $item.Trim()) {
            [void]$cleanInstalled.Add($item.Trim())
        }
    }

    return [pscustomobject]@{
        Enabled = $enabled
        Required = @($uniqueRequired)
        Installed = @($cleanInstalled)
    }
}

function Test-JarvisModelInstalled {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Wanted,
        [Parameter(Mandatory = $true)][string[]]$Installed
    )

    foreach ($candidate in $Installed) {
        if ($candidate -ieq $Wanted) {
            return $true
        }
        if (-not $Wanted.Contains(":") -and ($candidate -split ":", 2)[0] -ieq $Wanted) {
            return $true
        }
    }
    return $false
}

Write-Host "JARVIS Local setup"
Write-Host ""
Write-Host "[1/4] Checking Python..."
$pythonCommand = Get-Command -Name "python" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $pythonCommand) {
    throw "Python was not found. Install Python 3.11, 3.12, or 3.13 from https://www.python.org/downloads/windows/ and select 'Add python.exe to PATH', then rerun setup."
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
if ($pythonVersion -lt [version]"3.11" -or $pythonVersion -ge [version]"3.14") {
    throw "Python 3.11, 3.12, or 3.13 is required; found $pythonVersion."
}

Write-Host "Using Python $pythonVersion at $python"
Write-Host "[2/4] Installing JARVIS and its document-generation libraries..."
Write-Host "This public-preview installer uses the Python environment shown above; it does not create a virtual environment."
Invoke-NativeCommand -FilePath $python -ArgumentList @(
    "-X", "utf8", "-m", "pip", "install", "--disable-pip-version-check",
    "--no-input", "--editable", ".[documents]"
)

Write-Host "[3/4] Reviewing model-provider and optional-feature choices..."
Invoke-NativeCommand -FilePath $python -ArgumentList @(
    "-X", "utf8", "-m", "jarvis.provider_setup", "--interactive"
)

Invoke-NativeCommand -FilePath $python -ArgumentList @(
    "-X", "utf8", "-m", "jarvis.feature_onboarding", "--interactive"
)

$inventory = Get-JarvisModelInventory -PythonPath $python
$ollamaCommand = $null
if ($inventory.Enabled -and $inventory.Required.Count -gt 0) {
    $ollamaCommand = Get-Command -Name "ollama" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $ollamaCommand) {
        throw "Ollama is selected but was not found. Install it from https://ollama.com/download or rerun provider setup."
    }
}
$missingModels = @($inventory.Required | Where-Object {
    -not (Test-JarvisModelInstalled -Wanted "$_" -Installed $inventory.Installed)
})
foreach ($model in $missingModels) {
    Write-Host "Downloading configured model $model..."
    Invoke-NativeCommand -FilePath $ollamaCommand.Source -ArgumentList @("pull", $model)
}

if ($missingModels.Count -gt 0) {
    $verifiedInventory = Get-JarvisModelInventory -PythonPath $python
    $stillMissing = @($verifiedInventory.Required | Where-Object {
        -not (Test-JarvisModelInstalled -Wanted "$_" -Installed $verifiedInventory.Installed)
    })
    if ($stillMissing.Count -gt 0) {
        throw "Ollama did not report the downloaded model(s) as installed: $($stillMissing -join ', ')"
    }
}

Write-Host "[4/4] Verifying the installation..."
Write-Host "Checking that every configured model route can answer a first turn..."
Invoke-NativeCommand -FilePath $python -ArgumentList @(
    "-X", "utf8", "-m", "jarvis.provider_setup", "--canary"
)
Invoke-NativeCommand -FilePath $python -ArgumentList @("-X", "utf8", "-m", "jarvis", "doctor")
Write-Host ""
Write-Host "Ready. Double-click start_jarvis_presence.bat to open the recommended browser interface."
Write-Host "Terminal alternative: double-click start_jarvis.bat or run python -m jarvis"
