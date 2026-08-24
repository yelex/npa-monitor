#!/usr/bin/env bash
# Ретроактивная чистка сигналов на боевом сервере, PLAN.md Фаза 9 п.7,
# docs/SPEC_retroactive_signals_cleanup.md. Без флага --apply — только отчёт,
# БД не меняется.
#
# Запускать локально (не с этой sandbox-машины): у неё нет доступа к порту 22 сервера.
#
# Использование:
#   ./scripts/cleanup_signals.sh [user@]host [port] [--apply]
#   по умолчанию — 151.243.180.35:22, root (как в deploy.sh/dump_signals.sh)
#
# Перед --apply на боевой БД сделать бэкап файла (см. раздел 7 спеки):
#   ssh ... "cd /opt/npa-monitor && cp data/npa_monitor.db data/npa_monitor.db.bak-$(date +%F)"

set -euo pipefail

TARGET="${1:-151.243.180.35}"
PORT="${2:-22}"
APPLY_FLAG="${3:-}"

if [[ "$TARGET" == *"@"* ]]; then
  REMOTE_USER="${TARGET%%@*}"
  HOST="${TARGET##*@}"
else
  REMOTE_USER="${DEPLOY_USER:-root}"
  HOST="$TARGET"
fi

REMOTE_DIR="/opt/npa-monitor"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

ssh -p "$PORT" "${REMOTE_USER}@${HOST}" \
  "cd ${REMOTE_DIR} && docker compose exec -T bot python - ${APPLY_FLAG}" \
  < "${SCRIPT_DIR}/cleanup_signals.py"
