# SPEC: fix_review_75af72b — фиксы ревью write-back

## Статус
Черновик → в реализации.

## Контекст
Код-ревью коммита 75af72b (SPEC_result_edit) выявило 2 major-замечания.

## Проблема
1. **[major] HTML-инъекция в конфликте.** `bot/main.py:1611-1620` (`on_override_apply`): строка
   `f"«{c.field}»: ожидалось «{c.expected_was}», сейчас «{c.actual_value}»"` уходит в
   `parse_mode=HTML` без `html.escape`. Значения из текста НПА с `<`, `>`, `&` ломают
   `sendMessage` — аналитик не видит объяснение конфликта.
2. **[major] Ложный STALE-негатив из-за lru_cache.** `db/measures.py` кэширует
   `load_raw_record` (`lru_cache(maxsize=4)` по пути). `apply_selection` читает `kb_row`
   через этот кэш; `export_kb` перезаписывает файл, но кэш не сбрасывает → со второго
   «Применить» в рамках жизни процесса `base_row_hash` сравнивается с замороженным
   снапшотом → возможен ложноотрицательный STALE (спека §3.3 нарушена).

## Предполагаемый фикс
1. Экранировать `field`, `expected_was`, `actual_value` через `html.escape(..., quote=False)`
   в сообщении конфликта (по образцу `_fmt_change_value`).
2. В `scripts/export_kb.py` (и в месте вызова автоэкспорта в `bot/main.py`) после записи
   файла сбрасывать кэш: `db.measures.invalidate_kb_cache()` — добавить явную функцию,
   дергающую `load_raw_record.cache_clear()` и `kb_field_names.cache_clear()`.
   Не полагаться на одночастичную инвалидацию.

## Приёмка
- Новые тесты: (a) конфликт со значением `<b>&` не ломает отправку сообщения — текст
  экранирован; (b) unit: после `export_kb` в том же процессе `apply_selection` видит
  обновлённый `row_hash` (STALE срабатывает на устаревший base_row_hash).
- `python -m pytest` — без новых падений (2 известных stale-теста cntd.ru не в счёт).

## Как воспроизвести
- (a) применить батч с конфликтом, где actual_value содержит `<`; без фикса Telegram
  отвечает 400 "can't parse entities".
- (b) в одном процессе: export_kb → изменить base_row_hash в карточке → apply_selection
  без инвалидации вернёт stale=False; с фиксом — True.

## Backlog (не в этом фиксе)
- тест лимита 64 байта для `ovr:`/`ovrap:` callback_data;
- бот-тесты `_format_apply_result` для stale/rejected_unwhitelisted;
- try/except вокруг автоэкспорта в `on_override_apply` с понятным сообщением об ошибке.
