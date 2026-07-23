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
$dist = Join-Path $root "dist"
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

if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force }
if (Test-Path -LiteralPath $checksum) { Remove-Item -LiteralPath $checksum -Force }
New-Item -ItemType Directory -Path $dist -Force | Out-Null

$manifest = [ordered]@{ version = $Version; commit = $commit } | ConvertTo-Json -Compress
$manifestDirectory = Join-Path ([IO.Path]::GetTempPath()) ("contentai-package-" + [Guid]::NewGuid().ToString("N"))
$manifestFile = Join-Path $manifestDirectory "release-manifest.json"

try {
  New-Item -ItemType Directory -Path $manifestDirectory -Force | Out-Null
  $encoding = New-Object Text.UTF8Encoding($false)
  [IO.File]::WriteAllText($manifestFile, $manifest, $encoding)
  $archiveArguments = @(
    "archive",
    "--format=tar.gz",
    "--prefix=$name/",
    "--output=$archive",
    "--add-file=$manifestFile",
    $commit,
    "--"
  ) + $requiredPaths
  & git -C $root @archiveArguments
  if ($LASTEXITCODE -ne 0) { throw "Unable to create the release archive." }
}
finally {
  if (Test-Path -LiteralPath $manifestFile) { Remove-Item -LiteralPath $manifestFile -Force }
  if (Test-Path -LiteralPath $manifestDirectory) { Remove-Item -LiteralPath $manifestDirectory -Force }
}
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
"$hash  $name.tar.gz" | Set-Content -LiteralPath $checksum -Encoding ASCII -NoNewline
Write-Output $archive
Write-Output $checksum
Write-Output $hash
