param([string]$Version)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$commit = (& git -C $root rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($commit)) {
  throw "Unable to resolve the release commit."
}
$projectText = ((& git -C $root show "${commit}:pyproject.toml") -join [Environment]::NewLine)
if ($LASTEXITCODE -ne 0) { throw "Unable to read pyproject.toml from the release commit." }
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
  & git -C $root cat-file -e "${commit}:$relative"
  if ($LASTEXITCODE -ne 0) {
    throw "Required archive input is missing from the release commit: $relative"
  }
}

if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force }
if (Test-Path -LiteralPath $checksum) { Remove-Item -LiteralPath $checksum -Force }
New-Item -ItemType Directory -Path $stage -Force | Out-Null

$treeArchive = Join-Path (Split-Path $stage -Parent) (".contentai-tree-{0}.tar" -f [guid]::NewGuid().ToString("N"))
& git -C $root archive --format=tar --output=$treeArchive $commit -- $requiredPaths
if ($LASTEXITCODE -ne 0) { throw "Unable to materialize the release commit tree." }
try {
  & tar -xf $treeArchive -C $stage
  if ($LASTEXITCODE -ne 0) { throw "Unable to extract the release commit tree." }
} finally {
  if (Test-Path -LiteralPath $treeArchive) { Remove-Item -LiteralPath $treeArchive -Force }
}

foreach ($relative in $requiredPaths) {
  if (-not (Test-Path -LiteralPath (Join-Path $stage $relative))) {
    throw "Required archive input is missing after tree materialization: $relative"
  }
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
