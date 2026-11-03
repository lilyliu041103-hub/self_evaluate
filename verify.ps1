param([string]$Out)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$envPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $envPython)) { throw 'Run .\setup.ps1 first.' }
if (-not $Out) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $Out = Join-Path $projectRoot "outputs\verification\$stamp"
} elseif (-not [IO.Path]::IsPathRooted($Out)) {
    $Out = Join-Path $projectRoot $Out
}
if (Test-Path -LiteralPath $Out) { throw 'Use a new output directory; earlier verification records are preserved.' }
New-Item -ItemType Directory -Path $Out | Out-Null
Push-Location $projectRoot
try {
    & $envPython -m unittest discover -s tools/eval_bridge/tests -v *> (Join-Path $Out 'test-results.txt')
    if ($LASTEXITCODE -ne 0) { throw "Tests failed. See $Out\test-results.txt" }
    & $envPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed.' }
    Write-Host "Verification passed (0 paid API calls). Records: $Out"
} finally { Pop-Location }
