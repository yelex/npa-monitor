# SPEC: Обход pravo.gov.ru по календарным дням (periodType=day&date=)

## Статус
- [x] Implemented (02.09.2026)

## Контекст
Диагностика 02.09 (/tmp/claude_pravo_diag.md, логи ретро-тестов): `periodType=daily/
weekly/monthly` на pravo.gov.ru — это ФИКСИРОВАННЫЙ текущий календарный период
(сегодня / неделя Пн–Вс / текущий месяц), а не окно «последние N дней». При простое
дольше текущей недели/месяца документы пропущенного периода физически отсутствуют
в выдаче на любой странице `index`. Доказано живьём:
- monthly 02.09: 639 доков, все 01–02.09, стр. 23+ пустые; август недостижим.
- Кейс 412-П (№ 4100202608240009, опубл. 24.08): недостижим через weekly/monthly
  02.09 при любом window_start; при этом `periodType=day&date=24.08.2026` отдаёт
  его на стр. 5/5 дня (~150 доков).
- Параметр `date` игнорируется для weekly/monthly, работает только с `day`
  (проверено до 10.03.2023).
- Доп. канал: `/getcalendar/{block}?month=M&year=Y` — количества актов по дням
  месяца (можно дёшево пропускать пустые дни). Для block= (общий) не проверялся.

Также найден третий канал тихой потери: обход может «успешно» пробежать monthly до
чистой пустой страницы и пометить источник обработанным (`window_covered=True`),
хотя документы пропущенного периода ни разу не были в выдаче.

## Проблема
Эскалация daily→weekly→monthly (SPEC_pravo_gov_pagination_depth, ccb7236) не
доверстывает окна старше текущей недели/месяца. «Успех» weekly 28.08 с 412-П на
стр. 71 был совпадением дат, а не работой эскалации.

## Предполагаемый фикс
1. **Обход по дням**: в `parser/sources/pravo_gov.py` — генерация плана дней
   `[window_start.date() .. now.date()]` (МСК), для каждого дня пагинация
   `Documents/search?block=&periodType=day&date=DD.MM.YYYY&index=N`, стоп:
   пустая страница / `published_at < window_start` (защита от мусорных дат) /
   per-day max_pages (текущий лимит 20 для daily достаточен: живьём ~150 доков/день
   = 5–6 стр.; поднять до 30 с запасом).
2. **Календарь-оптимизация (опционально, off по умолчанию)**: `/getcalendar` для
   пропуска дней с 0 актов. Только если block= подтверждён рабочим; иначе не городить.
3. **select_period упраздняется** для pravo.gov (остаётся для совместимости тестов
   или удаляется вместе с PRAVO_GOV_MAX_PAGES_BY_PERIOD; weekly/monthly пути
   больше не используются).
4. Дни с частичным покрытием (сработал max_pages до конца дня) — не терять:
   обрабатывать следующий день от последней полностью покрытой даты
   (`last_seen_publication_date` уже есть в SourceState), источник не помечать
   обработанным.
5. Стоп-условие «старше окна» и window_covered-логика оркестратора остаются как есть.

## Как воспроизвести
- Юнит: window 24.08–02.09 → план 10 дней, запросы с date=24.08.2026...02.09.2026;
  412-П (фixture стр.5 дня 24.08) находится.
- Живо (после деплоя): тестовая копия БД, window_start=24.08 → 412-П в signals.

## Вне scope
- /getcalendar (если не подтвердится для block=); rate-limit днями (прогон ночью
  и так один); другие источники.

## Реализовано (02.09.2026)

Всё — в `parser/orchestrator.py` и `parser/sources/pravo_gov.py`.

1. **`parser/sources/pravo_gov.py::build_day_plan(window_start, now)`** — чистая
   функция даты (без сетевого вызова): список календарных дней МСК от
   `window_start.date()` до `now.date()` включительно, по возрастанию.
   `fetch_documents` принял новый kwarg `date: str | None` (`DD.MM.YYYY`) —
   добавляется в URL как `&date=...`, если задан; `period` по умолчанию — `"day"`
   (было `"daily"`). `select_period` удалён (не оставлен для совместимости — не было
   внешних вызывающих кроме `orchestrator.py`, который переписан вместе с ним).

2. **`parser/orchestrator.py`** — `SourceSpec` получил `day_plan: Callable[[window_start,
   now], list[date]] | None` и `day_max_pages: int = PRAVO_GOV_DAY_MAX_PAGES` (30, замена
   `PRAVO_GOV_MAX_PAGES`/`PRAVO_GOV_MAX_PAGES_BY_PERIOD`, оба удалены). Поля
   `max_pages_by_period`/`resolve_period`/`period_fallback` — удалены целиком вместе с
   веткой обработки в `process_source`: они использовались исключительно эскалацией
   periodType, единственным вызывающим кодом были специфика pravo_gov в
   `build_source_specs` и тесты этой ветки — после перехода на дни оба стали мёртвым
   кодом.

   `process_source` разделён на два хелпера по стратегии обхода:
   - `_process_source_paginated` — прежняя постраничная логика (стоп по дате/
     `window_tolerance`/недатированной странице/предохранителю `max_pages`) для всех
     источников без `day_plan`, поведение не изменилось (тесты
     `test_process_source_stops_pagination_at_window_start` и соседние — без изменений).
   - `_process_source_by_day` — новая ветка для источников с `day_plan`: каждый день
     плана пагинируется независимо (`fetch_page(page=N, period="day", date=DD.MM.YYYY)`)
     до пустой страницы или своего предохранителя `day_max_pages`; **окно считается
     покрытым только если КАЖДЫЙ день дошёл до естественного конца** — иначе источник не
     отмечается обработанным (`mark_source_processed` не вызывается), весь диапазон дней
     повторяется на следующем прогоне целиком (не только недостающий день — см. «Не
     реализовано намеренно» ниже). Публикации уже обойдённых дней (в т.ч. неполных) не
     теряются — сигналы создаются как обычно, откладывается только отметка успеха.
     Стоп по `published_at < window_start` внутри дня не применяется: сервер уже
     фильтрует по конкретной дате, дополнительная проверка не нужна и не имеет смысла
     (публикации на дне не отсортированы по времени суток, только по дате).

   `build_source_specs`: спека pravo_gov теперь — `day_plan=pravo_gov.build_day_plan,
   day_max_pages=PRAVO_GOV_DAY_MAX_PAGES`, без `max_pages`/`window_tolerance`/
   `resolve_period`/`period_fallback`.

3. **Тесты:**
   - `tests/test_sources_pravo_gov.py::test_build_day_plan` (параметризованный, границы:
     окно=0, окно внутри дня, окно через полночь МСК, длинный простой на 10 дней) и
     `test_build_day_plan_converts_non_moscow_tz_to_moscow_calendar_day` — заменили
     `test_select_period_by_window_size`. `test_fetch_documents_parses_real_markup_fragment`
     переписан на `period="day", date="19.08.2026"` (та же живая вёрстка).
   - `tests/test_orchestrator.py`:
     `test_process_source_day_plan_paginates_each_day_with_date_kwarg` —
     `day_plan` вызывается один раз, каждый день пагинируется отдельно с правильными
     kwargs; `test_process_source_day_plan_not_marked_processed_when_one_day_hits_safety_valve`
     — один «незакрывающийся» день не даёт источнику отметиться обработанным, публикации
     остальных дней при этом не теряются. Заменили 4 теста эскалации periodType
     (`test_process_source_adaptive_period_passed_to_fetch_page` и три соседних).
     `test_process_source_uses_per_source_max_pages_override` /
     `test_process_source_does_not_mark_processed_when_max_pages_safety_valve_hit` /
     `test_process_source_window_tolerance_allows_slightly_stale_items` — не трогались
     (генерическая механика `_process_source_paginated`, не завязана на pravo_gov).

Полный прогон (`PYTHONPATH=. .venv/bin/pytest -q`, 02.09.2026): 439 passed, 2 failed —
оба про `docs.cntd.ru` (`test_catalog.py::test_access_for_domain_returns_access_by_exact_or_subdomain_match`,
`test_bot_main.py::test_on_npa_link_accepts_unsupported_domain_on_trust_without_network_call`),
воспроизводятся на чистом `main` без изменений этой спеки (проверено `git stash`),
вне скоупа.

### Не реализовано намеренно
- **Резюмирование с последнего покрытого дня** (п.4 черновика — «обрабатывать следующий
  день от последней полностью покрытой даты», используя `last_seen_publication_date`).
  Реализовано более простое и однозначное правило «весь диапазон дней целиком, пока не
  покрыт полностью» (общий паттерн с уже существующим `_process_source_paginated`/
  `max_pages` safety-valve, ничего нового изобретать не пришлось). Дороже по числу HTTP-
  запросов при повторяющемся частичном покрытии одного и того же дня (маловероятно —
  `day_max_pages=30` даёт запас ~900 док/день против живых ~150), но не требует нового
  пути записи состояния источника отдельно от `mark_source_processed`, и дедуп по URL
  делает повтор уже обойдённых дней дёшевым. Пересмотреть, если в проде окажется, что
  частичное покрытие одного дня — частый случай, не только пограничный.
- `/getcalendar` — как и было решено в черновике, не подтверждено для `block=`, не в
  реализации.

## Ревью
- [ ] claude -p (после реализации)
