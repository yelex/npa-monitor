#!/usr/bin/env bash
# Забирает сигналы с боевого сервера и печатает их в терминал.
# Запускать локально (не с этой sandbox-машины): у неё нет доступа к порту 22 сервера.
#
# Использование:
#   ./scripts/dump_signals.sh [user@]host [port]
#   по умолчанию — 151.243.180.35:22, root (как в deploy.sh)

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
  < "${SCRIPT_DIR}/dump_signals.py"
