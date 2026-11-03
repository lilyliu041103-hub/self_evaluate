param([string]$Python)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$envPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $envPython)) {
    if ($Python) {
        & $Python -m venv (Join-Path $projectRoot '.venv')
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3.12 -m venv (Join-Path $projectRoot '.venv')
    } else {
        throw 'Install Python 3.12, then run setup.ps1 -Python <path-to-python.exe>.'
    }
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment.' }
}
& $envPython -m pip install -r (Join-Path $projectRoot 'requirements.lock.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $envPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency consistency check failed.' }
Write-Host 'Environment ready. Run .\evaluate.ps1 --help or .\verify.ps1.'
