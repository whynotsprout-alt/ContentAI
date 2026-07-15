param(
  [switch]$SkipFrontend
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

if (!(Test-Path $VenvPython)) {
  python -m venv (Join-Path $Root ".venv")
}

& $VenvPython -m pip install --upgrade pip
$EditableTarget = "${Root}[dev]"
& $VenvPython -m pip install -e $EditableTarget

if (!$SkipFrontend) {
  Push-Location (Join-Path $Root "apps/web")
  npm.cmd install
  Pop-Location
}

Write-Host "contentai setup complete."
