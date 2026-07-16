$Action = $args[0]
$EnvFile = Join-Path $env:TEMP "contentai-test-deps.env"
$Compose = @("--env-file", $EnvFile, "-p", "contentai-test-deps", "-f", "compose.yaml", "-f", "compose.dev.yaml")

if ($Action -eq "up") {
  @'
POSTGRES_DB=contentai_test
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_PORT=5432
REDIS_PORT=6379
WEB_PORT=5190
API_PORT=8010
CONTENTAI_DATABASE__URL=postgresql+psycopg://postgres:postgres@postgres:5432/contentai_test
'@ | Set-Content -Encoding utf8 $EnvFile
  docker compose @Compose up --detach --wait postgres
} elseif ($Action -eq "down") {
  docker compose @Compose down --volumes --remove-orphans
  Remove-Item -Force -ErrorAction SilentlyContinue $EnvFile
} else {
  throw "Usage: tools/test-deps.ps1 up|down"
}
