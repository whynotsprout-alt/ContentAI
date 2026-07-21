param([string]$Version)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$projectText = Get-Content -LiteralPath (Join-Path $root "pyproject.toml") -Raw
$projectBlock = [regex]::Match($projectText, '(?ms)^\[project\]\s*(?<body>.*?)(?=^\[|\z)')
$versionMatch = [regex]::Match($projectBlock.Groups['body'].Value, '(?m)^version\s*=\s*"(?<version>[^"]+)"\s*$')
if (-not $versionMatch.Success) { throw "Canonical project version is missing from pyproject.toml" }
$canonicalVersion = $versionMatch.Groups['version'].Value
if ([string]::IsNullOrWhiteSpace($Version)) {
  $Version = $canonicalVersion
} elseif ($Version -ne $canonicalVersion) {
  throw "Package version '$Version' does not match canonical version '$canonicalVersion'."
}
if ($Version -notmatch '^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$') {
  throw "Package version is invalid: $Version"
}
$name = "contentai-$Version-ubuntu"
$stage = Join-Path $root "dist/$name"
$archive = Join-Path $root "dist/$name.tar.gz"
$checksum = "$archive.sha256"

$requiredPaths = @(
  "apps/api/src", "apps/web/src", "apps/web/index.html",
  "apps/api/src/contentai_migrations/env.py",
  "apps/api/src/contentai_migrations/versions/202607210001_v050_initial_schema.py",
  "apps/web/package.json", "apps/web/package-lock.json", "apps/web/tsconfig.json",
  "apps/web/tsconfig.node.json", "apps/web/vite.config.ts",
  "docs", "infra", "infra/ubuntu/deploy.sh", "infra/ubuntu/health.sh",
  "infra/ubuntu/backup.sh", "infra/ubuntu/restore.sh", "infra/ubuntu/upgrade.sh",
  "tools/package-ubuntu.ps1",
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
if (Test-Path -LiteralPath $checksum) { Remove-Item -LiteralPath $checksum -Force }
New-Item -ItemType Directory -Path $stage -Force | Out-Null

foreach ($relative in $requiredPaths) {
  $source = Join-Path $root $relative
  $target = Join-Path $stage $relative
  New-Item -ItemType Directory -Path (Split-Path $target -Parent) -Force | Out-Null
  Copy-Item -LiteralPath $source -Destination $target -Recurse -Force
}

$commit = (& git -C $root rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($commit)) {
  throw "Unable to resolve the release commit."
}
[ordered]@{ version = $Version; commit = $commit } |
  ConvertTo-Json |
  Set-Content -LiteralPath (Join-Path $stage "release-manifest.json") -Encoding UTF8

Get-ChildItem -Path $stage -Recurse -Directory | Where-Object {
  $_.Name -in @("node_modules", "dist", "__pycache__", ".pytest_cache", ".ruff_cache")
} | Sort-Object FullName -Descending | Remove-Item -Recurse -Force

tar -C (Split-Path $stage -Parent) -czf $archive $name
if ($LASTEXITCODE -ne 0) { throw "tar 打包失败" }
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
"$hash  $name.tar.gz" | Set-Content -LiteralPath $checksum -Encoding ASCII -NoNewline
Write-Output $archive
Write-Output $checksum
Write-Output $hash
