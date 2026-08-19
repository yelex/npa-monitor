#!/usr/bin/env bash
# Ralph loop: повторно запускает Claude Code в headless-режиме над PLAN.md,
# пока не выполнены все задачи (маркер RALPH_DONE) или не достигнут лимит
# итераций/бюджета.
#
# ВНИМАНИЕ:
#   --dangerously-skip-permissions отключает все запросы подтверждения —
#   Claude сможет менять файлы, коммитить, запускать команды без вопросов.
#   Использовать только в одноразовом/изолированном окружении (отдельный
#   worktree/ветка/контейнер), не на машине с важными несохранёнными данными.
#
# Перед первым запуском:
#   1. git init && git add -A && git commit -m "initial state" (если ещё не
#      сделано) — каждая итерация коммитит поверх текущего состояния, откат
#      делается через git.
#   2. Проверь MAX_ITERATIONS и MAX_BUDGET_USD ниже — это единственная защита
#      от бесконечного/неконтролируемого расхода.
#   3. Прогони одну итерацию вручную (без цикла) и посмотри на диff/коммит,
#      прежде чем оставлять скрипт работать без присмотра.

set -euo pipefail
cd "$(dirname "$0")"

PROMPT_FILE="RALPH_PROMPT.md"
PLAN_FILE="PLAN.md"
LOG_DIR="ralph_logs"
MAX_ITERATIONS="${RALPH_MAX_ITERATIONS:-50}"
MAX_BUDGET_USD_PER_ITERATION="${RALPH_MAX_BUDGET_USD:-2}"
SLEEP_BETWEEN_SECONDS="${RALPH_SLEEP_SECONDS:-5}"

if [ ! -d .git ]; then
  echo "Нет git-репозитория в $(pwd). Инициализируй git (git init + первый коммит) перед запуском ralph-цикла." >&2
  exit 1
fi

if [ ! -f "$PROMPT_FILE" ] || [ ! -f "$PLAN_FILE" ]; then
  echo "Не найден $PROMPT_FILE или $PLAN_FILE рядом со скриптом." >&2
  exit 1
fi

mkdir -p "$LOG_DIR"

for ((i = 1; i <= MAX_ITERATIONS; i++)); do
  ts="$(date +%Y%m%d_%H%M%S)"
  log_file="${LOG_DIR}/iter_${i}_${ts}.log"

  echo "=== Ralph iteration ${i}/${MAX_ITERATIONS} (${ts}) ==="

  set +e
  claude -p "$(cat "$PROMPT_FILE")" \
    --dangerously-skip-permissions \
    --max-budget-usd "$MAX_BUDGET_USD_PER_ITERATION" \
    --output-format text \
    2>&1 | tee "$log_file"
  status=${PIPESTATUS[0]}
  set -e

  if [ "$status" -ne 0 ]; then
    echo "Итерация ${i} завершилась с ошибкой (exit ${status}). Смотри ${log_file}. Останавливаюсь." >&2
    exit "$status"
  fi

  if grep -q "RALPH_DONE" "$log_file"; then
    echo "Ralph сообщил, что все задачи в ${PLAN_FILE} выполнены/заблокированы. Останавливаюсь."
    exit 0
  fi

  sleep "$SLEEP_BETWEEN_SECONDS"
done

echo "Достигнут лимит итераций (${MAX_ITERATIONS}), но RALPH_DONE не получен. Проверь ${PLAN_FILE} и логи в ${LOG_DIR}/ вручную." >&2
exit 2
