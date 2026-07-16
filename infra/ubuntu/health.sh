#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

ENV_FILE="${CONTENTAI_ENV_FILE:-$ROOT/.env}"
COMPOSE=(docker compose --env-file "$ENV_FILE")
"${COMPOSE[@]}" ps --all
"${COMPOSE[@]}" exec -T api python -c \
  "import json,urllib.request; print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/ready', timeout=5)), ensure_ascii=False, indent=2))"
