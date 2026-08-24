#!/usr/bin/env bash
# Деплой npa-monitor на боевой VPS через Docker (README.md, раздел «Деплой»;
# AGENTS.md раздел 12).
#
# Синхронизирует рабочее дерево репозитория (не последний git-коммит — намеренно:
# на момент написания в дереве есть незакоммиченные правки), поднимает `bot` как
# долгоживущий сервис и настраивает cron для ежедневного `parser` в 06:00.
#
# Использование:
#   ./deploy.sh [user@]host [port]
#   DEPLOY_USER=ubuntu ./deploy.sh 151.243.180.35 22
#
# По умолчанию — сервер 151.243.180.35:22, пользователь root (переопределяется
# через DEPLOY_USER или явно указав user@host первым аргументом).

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
SSH=(ssh -p "$PORT" "${REMOTE_USER}@${HOST}")

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -f .env ]]; then
  echo "Локальный .env не найден — заполните его перед деплоем (см. .env.example)." >&2
  exit 1
fi

echo "==> Проверка SSH-доступа к ${REMOTE_USER}@${HOST}:${PORT}"
"${SSH[@]}" "echo ok" >/dev/null

echo "==> Установка Docker на сервере (если ещё не установлен)"
"${SSH[@]}" "command -v docker >/dev/null 2>&1 || curl -fsSL https://get.docker.com | sh"

echo "==> Установка rsync на сервере (если ещё не установлен — нужен на обоих концах)"
"${SSH[@]}" "command -v rsync >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq rsync)"

echo "==> Создание директории проекта на сервере"
"${SSH[@]}" "mkdir -p ${REMOTE_DIR}"

echo "==> Синхронизация кода (рабочее дерево, включая незакоммиченные правки; без .venv/.git/данных)"
rsync -az --delete \
  --exclude-from=.gitignore \
  --exclude=.git \
  --exclude=.env \
  --exclude=.DS_Store \
  -e "ssh -p ${PORT}" \
  ./ "${REMOTE_USER}@${HOST}:${REMOTE_DIR}/"

echo "==> Копирование .env (секреты, не в git — передаётся отдельно)"
scp -P "$PORT" .env "${REMOTE_USER}@${HOST}:${REMOTE_DIR}/.env"

echo "==> Сборка образа"
"${SSH[@]}" "cd ${REMOTE_DIR} && docker compose build"

echo "==> Остановка предыдущего контейнера bot на сервере (если был)"
"${SSH[@]}" "cd ${REMOTE_DIR} && docker compose stop bot 2>/dev/null || true"

echo "==> Запуск бота (docker compose up -d bot)"
"${SSH[@]}" "cd ${REMOTE_DIR} && docker compose up -d bot"

echo "==> Установка cron на сервере (если ещё не установлен — минимальные образы его не включают)"
"${SSH[@]}" "command -v crontab >/dev/null 2>&1 || (apt-get update -qq && apt-get install -y -qq cron && (systemctl enable --now cron 2>/dev/null || service cron start))"

echo "==> Настройка cron для ежедневного парсера (06:00)"
CRON_LINE="0 6 * * * cd ${REMOTE_DIR} && docker compose run --rm parser >> /var/log/npa-monitor-parser.log 2>&1"
"${SSH[@]}" "(crontab -l 2>/dev/null | grep -vF 'npa-monitor-parser.log'; echo '${CRON_LINE}') | crontab -"

echo
echo "==> Готово. Логи бота (последние 20 строк):"
"${SSH[@]}" "cd ${REMOTE_DIR} && docker compose logs --tail=20 bot"

echo
echo "ВАЖНО:"
echo "1. У Telegram-бота с одним токеном не может быть двух одновременных long-polling"
echo "   процессов — если бот сейчас ещё запущен локально (см. /tmp/bot_run.log),"
echo "   остановите его: kill \$(pgrep -f '.venv/bin/python -m bot')"
echo "2. IP сервера (${HOST}) должен быть в allow-листе RU-прокси (RU_PROXY_URL в .env)"
echo "   для доступа к kremlin.ru/government.ru/publication.pravo.gov.ru — см. README,"
echo "   раздел «Деплой», и AGENTS.md раздел 16 п.7. Проверить:"
echo "   ${SSH[*]} 'cd ${REMOTE_DIR} && docker compose run --rm parser -v'"
