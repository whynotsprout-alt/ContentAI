param([string]$Version = "0.4.0")

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$name = "contentai-$Version-ubuntu"
$stage = Join-Path $root "dist/$name"
$archive = Join-Path $root "dist/$name.tar.gz"

$requiredPaths = @(
  "apps/api/src", "apps/web/src", "apps/web/index.html",
  "apps/web/package.json", "apps/web/package-lock.json", "apps/web/tsconfig.json",
  "apps/web/tsconfig.node.json", "apps/web/vite.config.ts",
  "docs", "infra", "tools/package-ubuntu.ps1",
  "compose.yaml", "pyproject.toml", "uv.lock", "requirements.txt",
  "alembic.ini", ".env.example", ".dockerignore", "README.md"
)

foreach ($relative in $requiredPaths) {
  if (-not (Test-Path -LiteralPath (Join-Path $root $relative))) {
    throw "Required archive input is missing: $relative"
  }
}

if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force }
New-Item -ItemType Directory -Path $stage -Force | Out-Null

foreach ($relative in $requiredPaths) {
  $source = Join-Path $root $relative
  $target = Join-Path $stage $relative
  New-Item -ItemType Directory -Path (Split-Path $target -Parent) -Force | Out-Null
  Copy-Item -LiteralPath $source -Destination $target -Recurse -Force
}

Get-ChildItem -Path $stage -Recurse -Directory | Where-Object {
  $_.Name -in @("node_modules", "dist", "__pycache__", ".pytest_cache", ".ruff_cache")
} | Sort-Object FullName -Descending | Remove-Item -Recurse -Force

tar -C (Split-Path $stage -Parent) -czf $archive $name
if ($LASTEXITCODE -ne 0) { throw "tar 打包失败" }
Write-Output $archive
Get-FileHash -Algorithm SHA256 -LiteralPath $archive | Select-Object -ExpandProperty Hash
