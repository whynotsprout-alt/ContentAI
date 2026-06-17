$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

if (!(Test-Path $VenvPython)) {
  throw "Missing .venv. Run scripts/setup.ps1 first."
}

$env:PYTHONPATH = Join-Path $Root "backend/src"
$Port = if ($env:ARTICLEFORGE_API_PORT) { $env:ARTICLEFORGE_API_PORT } else { "8001" }
& $VenvPython -m uvicorn articleforgeai.main:app --host 127.0.0.1 --port $Port
