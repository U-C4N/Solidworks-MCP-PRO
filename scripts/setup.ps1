[CmdletBinding()]
param(
    [string]$Python = '',
    [switch]$Dev
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$EnvironmentRoot = Join-Path $ProjectRoot '.venv'
$EnvironmentPython = Join-Path $EnvironmentRoot 'Scripts\python.exe'

function Invoke-Checked {
    param([string]$Executable, [string[]]$Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed (exit $LASTEXITCODE): $Executable"
    }
}

function Test-CompatiblePython {
    param([string]$Executable, [string[]]$PrefixArguments = @())
    try {
        & $Executable @PrefixArguments -c 'import sys, struct; sys.exit(0 if sys.version_info >= (3, 11) and struct.calcsize("P") == 8 else 1)' 2>$null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}

if (-not (Test-Path -LiteralPath $EnvironmentPython -PathType Leaf)) {
    $Launcher = $null
    $LauncherArguments = @()
    if ($Python) {
        if (-not (Test-CompatiblePython -Executable $Python)) {
            throw '-Python must identify an installed 64-bit Python 3.11 or newer executable.'
        }
        $Launcher = $Python
    }
    else {
        $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
        if ($PyLauncher) {
            # Prefer stable versions with broad COM/package wheel availability.
            foreach ($VersionSelector in @('-3.12', '-3.13', '-3.11', '-3.14', '-3')) {
                if (Test-CompatiblePython -Executable $PyLauncher.Source -PrefixArguments @($VersionSelector)) {
                    $Launcher = $PyLauncher.Source
                    $LauncherArguments = @($VersionSelector)
                    break
                }
            }
        }
        if (-not $Launcher) {
            $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
            if ($PythonCommand -and (Test-CompatiblePython -Executable $PythonCommand.Source)) {
                $Launcher = $PythonCommand.Source
            }
        }
    }
    if (-not $Launcher) {
        throw 'Install 64-bit Python 3.11+ (3.12 recommended), then retry with -Python pointing to python.exe.'
    }
    Invoke-Checked -Executable $Launcher -Arguments ($LauncherArguments + @('-m', 'venv', $EnvironmentRoot))
}

if (-not (Test-CompatiblePython -Executable $EnvironmentPython)) {
    throw 'The existing .venv is not a compatible 64-bit Python 3.11+ environment. Choose a fresh project checkout or repair that environment explicitly.'
}

Push-Location -LiteralPath $ProjectRoot
try {
    $InstallArguments = @('-m', 'pip', 'install', '.')
    if ($Dev) {
        $InstallArguments = @('-m', 'pip', 'install', '-e', '.[dev]')
    }
    Invoke-Checked -Executable $EnvironmentPython -Arguments $InstallArguments
    Invoke-Checked -Executable $EnvironmentPython -Arguments @('scripts/configure_clients.py')
}
finally {
    Pop-Location
}

Write-Host 'Installation complete. Review artifacts/configs and docs/clients.md to connect a client.'
Write-Host 'No global client settings were changed.'
