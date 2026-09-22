# SPEC_autocomplete_and_drop_tg_confirmation: авто-complete по маркеру v4, удаление TG-контура подтверждения

## Статус
Проект (согласован с Алексеем 22.09: лёгкий маркер в спул, авто-complete, error → TG-уведомление админу).

## Контекст
Смежные спеки в npa-somas: `SPEC_drop_spool_result_confirmation.md`, `SPEC_done_marker_v4.md` (коннектор пишет в `results/<task_id>.json` лёгкий маркер v4: `{schema_version:4, task_id, signal_id, status: done|nothing_found|error, finished_at, error_message?}`), `SPEC_kb_ssot.md` (KB на общем volume `npa_data` → `/app/data/kb/`, review-ui — единственный писатель; write-back-контур npa-monitor умирает; прод-БД: `measure_overrides` = 0 — миграция не нужна).

Решения Алексея 22.09:
- Эксперт подтверждает изменения только через review-ui.
- `done|nothing_found` → бот авто-закрывает сигнал (Завершён).
- `error` → сигнал не закрывать + уведомление админу в TG (fire-and-forget, механизм уже есть).
- В `results/` старых v3-файлов нет (results пуст, 53 задачи в `tasks/`) — ветки v3 в боте не пишем.

## Проблема
Два параллельных пути подтверждения одного результата (TG-карточки/чекбоксы и review-ui); жизненный цикл сигнала завязан на ручной `/complete`.

## Предполагаемый фикс

### 1. Тонкий scan-loop вместо полного
`scan_autoupdate_results` переписывается:
- читает `results/*.json`; при `schema_version != 4` — warning + архивация без обработки (защита от неожиданных старых файлов);
- `status in (done, nothing_found)` → сигнал → `Завершён` (reason: `auto:<status>`, changed_by — служебное значение/админ);
- `status == error` → сигнал не трогать; TG админу fire-and-forget: task_id, signal_id, error_message, ссылка на сигнал;
- после обработки — архивация в `results/.processed/` (как сейчас);
- убрать: `result_card`, `changes_card_text`, `changes_kb`, отправку карточок экспертам, `upsert_signal_result`.

### 2. Удаление TG-контура диффа
- хендлеры `ovr:*` (`on_override_button`, `on_override_value`, FSM `ResultEditFlow`, «Применить» → `apply_selection`);
- `db/overrides.py` (`apply_selection`, `overridden_measure_ids`), бейдж «✏️ правлено» в `measure_label`;
- `scripts/export_kb.py` (write-back-экспорт KB — заменён review-ui/SSOT);
- модели `SignalResult` и `MeasureOverride` в БД оставить (не трогать миграции), потребителей удалить.

### 3. Не трогаем
- кладку задач в `tasks/` (приём коннектором);
- `/complete` как ручную команду (совместимость, админ может закрыть руками);
- `RESULTS_SCAN_INTERVAL` 300 c и каркас `_results_scan_loop`.

## Как воспроизвести-проверить
- `pytest tests/` — зелёный; тесты scan-loop переписать под маркер v4 (done/nothing_found закрывают, error шлёт админу, неизвестный schema_version архивируется молча).
- Ручной: положить в tmp spool `results/sig-test.json` с v4-маркером → сигнал закрыт, файл в `.processed/`; с `status:error` → сигнал открыт, админу ушло уведомление.
- `grep -rn "apply_selection\|changes_kb\|overridden_measure_ids" bot db scripts` — пусто.
