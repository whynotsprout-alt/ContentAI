$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

Push-Location $Root
try {
  docker compose -f compose.yaml -f compose.dev.yaml up --detach --build --remove-orphans --wait --wait-timeout 180
  Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/api/ready" | Out-Null
  Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:5180/" | Out-Null
  docker compose -f compose.yaml -f compose.dev.yaml ps
} finally {
  Pop-Location
}
