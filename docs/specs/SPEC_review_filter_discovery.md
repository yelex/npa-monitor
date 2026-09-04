# Спека: фильтр обзоров в Yandex-discovery (дубль фикса, пропущенный в discovery-пути)

Статус: **к реализации** (04.09, жалоба Алексея: сигнал #196 — обзор КонсультантПлюс по Рязанской области).

## Контекст

`docs/SPEC_no_reviews_no_stale_reminders.md` (коммит 2abcc0f) ввёл отсечение
REVIEW-публикаций, но фильтр вставлен только в `parser/orchestrator.py`
(`_process_publication`, строка ~364). Обход источников — не единственный путь
создания сигналов: есть второй путь — Yandex-discovery
(`parser/discovery_search.py`, SPEC_yandex_search_discovery), который зовёт
`build_signal(session, pub, trace.result)` напрямую (строка ~350) — без проверки
`event_type == EventType.REVIEW`.

## Проблема

Обзоры/агрегаторы, найденные через Yandex-discovery (например,
`consultant.ru/law/review/reg/...` — «Новое в законодательстве Рязанской области»),
становятся сигналами. В БД сейчас 222 REVIEW-сигнала из 311 — ~70% шума в рассылке.

Пример (сигнал #196, создан 02.09 06:22):
- title: «Новое в законодательстве Рязанской области. Выпуск за 28 августа 2026 года \ Обзоры законодательства \ КонсультантПлюс»
- url: https://www.consultant.ru/law/review/reg/rlaw/rlaw0732026-08-28.html
- priority: MEDIUM, status: NEW — попал в утренний digest.

## Предполагаемый фикс

1. В `parser/discovery_search.py` после `classifier.explain(pub)` (и после
   проверки `is_relevant`), перед `build_signal` — тот же фильтр, что в
   оркестраторе:
   ```python
   if trace.result.event_type == EventType.REVIEW:
       result.reviews += 1  # + поле в DiscoveryResult, если его нет
       continue
   ```
   Замечание: фильтр ставить ДО `_fetch_full_title` (зачем тянуть заголовок
   страницы, которая будет отброшена).
2. Рефакторинг (решение по ревью Клода 04.09 — обязательный, не опциональный):
   вынести условие `is_relevant and event_type == REVIEW` в общую функцию
   (например, в `parser/signals.py`), чтобы оркестратор и discovery использовали
   одну точку — фильтр уже один раз разъехался, дублировать нельзя.
3. Чистка мусора (одноразово): НЕ отдельный скрипт и НЕ произвольная строка в
   `rejection_reason` (грабли: в БД уже есть `cleanup_20260828`, не входящее в
   enum → LookupError при ORM-выборке, задокументировано в
   SPEC_vrf_tass_aggregator_filter). Вместо этого — новый шаг в существующем
   `scripts/cleanup_signals.py`: он уже ограничен ACTIVE_STATUSES=(NEW, POSTPONED)
   и работает через `transition_status(..., rejection_reason=RejectionReason.NOT_NPA)`;
   свободный текст — в аудит-поле `reason`. Отравленные REJECTED-строки не трогаем —
   они вне ACTIVE_STATUSES и в WHERE не попадут.

## Как воспроизвести

- Положить в Yandex-discovery фейковый результат с title «Новое в законодательстве
  X области. Выпуск за …» и url `consultant.ru/law/review/...` → без фикса
  создаётся сигнал, с фиксом — `result.reviews += 1`, сигнала нет.

## Приёмка

- Regression-тест именно в `tests/test_discovery_search.py`: discovery-путь на
  REVIEW-публикации не создаёт сигнал, на публикации с маркером события — создаёт
  (сейчас REVIEW в этом файле не покрыт вовсе).
- `python -m pytest` — без новых падений.
- Разовая чистка: после неё `_digest_signals` не возвращает REVIEW-сигналы.

## Backlog (не в этом фиксе)

- Решить, считать ли «обзорные» домены (`consultant.ru/law/review/`) отдельным
  блэклистом URL-паттернов — пока нет, по спеке 2abcc0f фильтр семантический.
