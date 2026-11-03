param([Parameter(ValueFromRemainingArguments = $true)][string[]]$CliArguments)
$ErrorActionPreference = 'Stop'
$envPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $envPython)) { throw 'Run .\setup.ps1 first.' }
if (-not $CliArguments) { $CliArguments = @('--help') }
Push-Location $PSScriptRoot
try {
    & $envPython (Join-Path $PSScriptRoot 'tools\evaluate_agentos.py') @CliArguments
    $commandExit = $LASTEXITCODE
} finally { Pop-Location }
exit $commandExit
