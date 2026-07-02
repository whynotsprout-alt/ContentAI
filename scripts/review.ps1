$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

if (!(Test-Path $VenvPython)) {
  throw "Missing .venv. Run scripts/setup.ps1 first."
}

$env:PYTHONPATH = Join-Path $Root "backend/src"
& $VenvPython -m ruff check backend/src backend/tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $VenvPython -m pytest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Push-Location (Join-Path $Root "frontend")
npm.cmd run build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Pop-Location
