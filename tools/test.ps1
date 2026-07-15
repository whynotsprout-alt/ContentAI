$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

if (!(Test-Path $VenvPython)) {
  throw "Missing .venv. Run tools/setup.ps1 first."
}

$env:PYTHONPATH = Join-Path $Root "apps/api/src"
& $VenvPython -m pytest
