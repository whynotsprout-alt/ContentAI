#!/usr/bin/env bash
set -euo pipefail

test $# -eq 1 || { echo "用法: $0 backups/contentai-*.sql.gz" >&2; exit 2; }
BACKUP="$(realpath "$1")"
test -f "$BACKUP" || { echo "备份不存在: $BACKUP" >&2; exit 2; }
CHECKSUM="$BACKUP.sha256"
test -f "$CHECKSUM" || { echo "校验和文件不存在: $CHECKSUM" >&2; exit 2; }
(cd "$(dirname "$BACKUP")" && sha256sum --check "$(basename "$CHECKSUM")")

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
ENV_FILE="${CONTENTAI_ENV_FILE:-$ROOT/.env}"
set -a
source "$ENV_FILE"
set +a
COMPOSE=(docker compose --env-file "$ENV_FILE")

echo "恢复会覆盖当前数据库。设置 CONTENTAI_CONFIRM_RESTORE=yes 后重试。" >&2
test "${CONTENTAI_CONFIRM_RESTORE:-}" = "yes" || exit 3
"${COMPOSE[@]}" stop api dispatcher agent-worker background-worker side-effect-worker beat
gzip -dc "$BACKUP" | "${COMPOSE[@]}" exec -T postgres psql \
  --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"
"${COMPOSE[@]}" run --rm migration
"${COMPOSE[@]}" up --detach --wait
