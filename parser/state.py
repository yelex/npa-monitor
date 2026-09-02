"""Доверстывание пропущенных периодов обхода, AGENTS.md раздел 4/5, PLAN.md Фаза 3.

Для каждого источника хранится дата последнего успешного обхода (`db.models.SourceState`).
Окно поиска публикаций — не жёстко «последние 24 часа», а «с даты последнего успешного
обхода» (AGENTS.md: «хранить дату последнего успешного обхода per-источник и от неё вести
окно поиска, а не жёстко "today only"»). Первый запуск источника — окно 7 календарных дней
(AGENTS.md раздел 4.5).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from db.models import SourceState
from db.service import record_source_failure, update_source_state

FIRST_RUN_WINDOW = dt.timedelta(days=7)


def fetch_window_start(
    session: Session, source_key: str, *, now: dt.datetime | None = None
) -> dt.datetime:
    """Начало окна поиска публикаций для источника.

    Источник встречается впервые (нет записи в `SourceState` или ещё не было успешного
    обхода) — окно `FIRST_RUN_WINDOW` (7 дней). Иначе — с даты последнего успешного
    обхода, сколько бы дней назад он ни был: так пропуск цикла из-за сбоя не теряет
    сигналы за пропущенный период.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    state = session.get(SourceState, source_key)
    if state is None or state.last_success_at is None:
        return now - FIRST_RUN_WINDOW
    return state.last_success_at


def mark_source_processed(
    session: Session,
    source_key: str,
    *,
    success_at: dt.datetime | None = None,
    last_seen_publication_date: dt.date | None = None,
) -> SourceState:
    """Отмечает успешный обход источника (обёртка над `db.service.update_source_state`)."""
    return update_source_state(
        session,
        source_key=source_key,
        success_at=success_at or dt.datetime.now(dt.timezone.utc),
        last_seen_publication_date=last_seen_publication_date,
    )


def mark_source_failed(
    session: Session, source_key: str, *, at: dt.datetime | None = None
) -> SourceState:
    """Отмечает неудачную попытку обхода источника (docs/SPEC_source_health_alert.md,
    обёртка над `db.service.record_source_failure`)."""
    return record_source_failure(
        session, source_key=source_key, attempt_at=at or dt.datetime.now(dt.timezone.utc)
    )
