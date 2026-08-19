"""Тесты parser/state.py (доверстывание), PLAN.md Фаза 3."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from db.session import init_db, make_engine, make_session_factory
from parser.state import FIRST_RUN_WINDOW, fetch_window_start, mark_source_processed


@pytest.fixture
def session(tmp_path) -> Session:
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        yield session


def test_fetch_window_start_is_seven_days_for_unknown_source(session: Session) -> None:
    now = dt.datetime(2026, 8, 20, 6, 0, tzinfo=dt.timezone.utc)

    window_start = fetch_window_start(session, "sfr.gov.ru/press_center/news", now=now)

    assert window_start == now - FIRST_RUN_WINDOW


def test_fetch_window_start_uses_last_success_after_mark_processed(session: Session) -> None:
    last_success = dt.datetime(2026, 8, 18, 6, 0, tzinfo=dt.timezone.utc)
    mark_source_processed(session, "mintrud.gov.ru/docs", success_at=last_success)

    window_start = fetch_window_start(
        session, "mintrud.gov.ru/docs", now=dt.datetime(2026, 8, 20, 6, 0, tzinfo=dt.timezone.utc)
    )

    assert window_start == last_success


def test_fetch_window_start_carries_over_missed_days_not_just_24h(session: Session) -> None:
    """Раздел 5 AGENTS.md: пропуск цикла (сбой 3 дня подряд) не должен терять сигналы —
    окно должно начинаться от последнего успеха, а не жёстко «последние 24 часа»."""
    last_success = dt.datetime(2026, 8, 15, 6, 0, tzinfo=dt.timezone.utc)
    mark_source_processed(session, "kremlin.ru/acts/news", success_at=last_success)

    now = dt.datetime(2026, 8, 20, 6, 0, tzinfo=dt.timezone.utc)  # 5 дней спустя
    window_start = fetch_window_start(session, "kremlin.ru/acts/news", now=now)

    assert window_start == last_success
    assert (now - window_start) > dt.timedelta(hours=24)


def test_mark_source_processed_updates_existing_record(session: Session) -> None:
    mark_source_processed(
        session, "mos.ru/authority/documents", success_at=dt.datetime(2026, 8, 18, tzinfo=dt.timezone.utc)
    )
    mark_source_processed(
        session, "mos.ru/authority/documents", success_at=dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc)
    )

    window_start = fetch_window_start(
        session, "mos.ru/authority/documents", now=dt.datetime(2026, 8, 20, 12, tzinfo=dt.timezone.utc)
    )

    assert window_start == dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc)
