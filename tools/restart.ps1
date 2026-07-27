$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Compose = @("-f", "compose.yaml", "-f", "compose.dev.yaml")
$SupportedRevision = "202607210001"

Push-Location $Root
try {
  $PostgresContainerId = ((& docker ps --filter "name=^/contentai-postgres-1$" --format "{{.ID}}" | Out-String).Trim())
  if ($LASTEXITCODE -eq 0 -and $PostgresContainerId) {
    $PostgresUser = ((& docker exec contentai-postgres-1 printenv POSTGRES_USER | Out-String).Trim())
    $PostgresDatabase = ((& docker exec contentai-postgres-1 printenv POSTGRES_DB | Out-String).Trim())
    $CurrentRevision = ((& docker exec contentai-postgres-1 psql -U $PostgresUser -d $PostgresDatabase -Atc "SELECT version_num FROM alembic_version LIMIT 1;" 2>$null | Out-String).Trim())
    if (
      $LASTEXITCODE -eq 0 -and
      $CurrentRevision -and
      $CurrentRevision -ne $SupportedRevision
    ) {
      throw "Existing database revision $CurrentRevision is not supported by this V0.5 rebuild. Create a fresh database or keep the compatible V0.4.3 deployment; no containers were changed."
    }
  }

  docker compose @Compose up --detach --build --remove-orphans --wait --wait-timeout 180
  Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:8000/api/ready" | Out-Null
  Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:5180/" | Out-Null
  docker compose @Compose ps
} finally {
  Pop-Location
}
