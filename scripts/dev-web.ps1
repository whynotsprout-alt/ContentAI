$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Push-Location (Join-Path $Root "frontend")
npm.cmd run dev -- --host 127.0.0.1 --port 5180 --strictPort
Pop-Location
