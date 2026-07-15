$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$LogDirectory = Join-Path $Root "logs"
$LogFile = Join-Path $LogDirectory "frontend.log"
New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
Push-Location (Join-Path $Root "apps/web")
npm.cmd run dev -- --host 127.0.0.1 --port 5180 --strictPort 2>&1 | Tee-Object -FilePath $LogFile -Append
Pop-Location
