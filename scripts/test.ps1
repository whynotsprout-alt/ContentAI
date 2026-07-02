$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

if (!(Test-Path $VenvPython)) {
  throw "Missing .venv. Run scripts/setup.ps1 first."
}

$env:PYTHONPATH = Join-Path $Root "backend/src"
& $VenvPython -m pytest
