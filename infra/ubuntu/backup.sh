#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
ENV_FILE="${CONTENTAI_ENV_FILE:-$ROOT/.env}"
set -a
source "$ENV_FILE"
set +a
COMPOSE=(docker compose --env-file "$ENV_FILE")

DEST="${1:-$ROOT/backups}"
mkdir -p "$DEST"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FILE="$DEST/contentai-$STAMP.sql.gz"
"${COMPOSE[@]}" exec -T postgres pg_dump \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --clean --if-exists \
  | gzip -9 > "$FILE"
test -s "$FILE"
sha256sum "$FILE" > "$FILE.sha256"
echo "$FILE"
