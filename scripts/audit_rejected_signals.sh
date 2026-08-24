#!/usr/bin/env bash
# Аналитическая сверка отклонённых сигналов на боевом сервере (только чтение,
# ничего не меняет), PLAN.md Фаза 9 п.7, docs/SPEC_retroactive_signals_cleanup.md,
# раздел 8.
#
# Запускать локально (не с этой sandbox-машины): у неё нет доступа к порту 22 сервера.
#
# Использование:
#   ./scripts/audit_rejected_signals.sh [user@]host [port]
#   по умолчанию — 151.243.180.35:22, root (как в deploy.sh/dump_signals.sh)

set -euo pipefail

TARGET="${1:-151.243.180.35}"
PORT="${2:-22}"

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
  "cd ${REMOTE_DIR} && docker compose exec -T bot python -" \
  < "${SCRIPT_DIR}/audit_rejected_signals.py"
