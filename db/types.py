"""Кастомные типы колонок SQLAlchemy, PLAN.md раздел 2.

`DateTime(timezone=True)` на SQLite не сохраняет `tzinfo` — значение записывается и
читается как naive datetime (проверено эмпирически при разработке `parser/state.py`,
Фаза 3: `SourceState.last_success_at` после `commit()`+свежего `session.get()` терял
`tzinfo`, хотя писался как UTC-aware). Это касается всех datetime-колонок схемы
(`Signal.created_at`/`updated_at`, `StatusHistory.changed_at`, `SourceState.
last_success_at`/`updated_at`, `DocumentSeen.first_seen_at`, `Expert.added_at`), не
только Фазы 3 — известное ограничение связки SQLAlchemy+SQLite, не баг конкретной
таблицы.

`UTCDateTime` хранит значение как naive UTC внутри БД и всегда отдаёт наружу aware
datetime (UTC) — так код, сравнивающий/вычисляющий разницу с этими полями (например
напоминание «сигнал >3 дней в работе», PLAN.md Фаза 6), может полагаться на
единообразные tz-aware значения независимо от бэкенда БД (SQLite сейчас, возможный
Postgres в будущем — PLAN.md раздел 2).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: dt.datetime | None, dialect: Dialect) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"UTCDateTime требует tz-aware datetime на входе, получено {value!r}")
        return value.astimezone(dt.timezone.utc)

    def process_result_value(self, value: dt.datetime | None, dialect: Dialect) -> dt.datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)
