$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

if (!(Test-Path $VenvPython)) {
  throw "Missing .venv. Run tools/setup.ps1 first."
}

$env:PYTHONPATH = Join-Path $Root "apps/api/src"
& $VenvPython -m ruff check apps/api/src apps/api/tests
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
uvx vulture apps/api/src --min-confidence 90
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $VenvPython -m pytest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Push-Location (Join-Path $Root "apps/web")
npm.cmd test
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
npm.cmd run build
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Pop-Location
