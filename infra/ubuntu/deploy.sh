#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

ENV_FILE="${CONTENTAI_ENV_FILE:-$ROOT/.env}"
test -f "$ENV_FILE" || { echo "缺少环境文件；请复制 .env.example 并填写生产配置。" >&2; exit 1; }
COMPOSE=(docker compose --env-file "$ENV_FILE")
docker compose version >/dev/null
"${COMPOSE[@]}" config --quiet
"${COMPOSE[@]}" build --pull
"${COMPOSE[@]}" up --detach --wait
"${COMPOSE[@]}" ps
