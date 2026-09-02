"""Тесты bot/health_alerts.py, docs/SPEC_source_health_alert.md."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from bot.health_alerts import (
    check_source_health,
    check_zero_signal_degradation,
    format_alert,
    format_zero_signal_alert,
    sources_due_for_alert,
    zero_signal_alert_due,
)
from db.enums import REGION_RF, EventType, Priority, SignalCategory
from db.service import create_signal, record_source_failure, update_source_state
from db.session import init_db, make_engine, make_session_factory

NOW = dt.datetime(2026, 9, 2, 8, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def session(tmp_path) -> Session:
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        yield session


def test_source_with_three_failed_attempts_triggers_alert(session: Session) -> None:
    """Спека, раздел «Реализация» п.3: «источник с 3 попытками без успеха → алерт»."""
    for i in range(3):
        record_source_failure(
            session,
            source_key="publication.pravo.gov.ru",
            attempt_at=NOW - dt.timedelta(hours=2 - i),
        )
    session.commit()

    unhealthy = check_source_health(session, now=NOW)

    assert len(unhealthy) == 1
    assert unhealthy[0].source_key == "publication.pravo.gov.ru"
    assert unhealthy[0].consecutive_failures == 3
    assert unhealthy[0].last_success_at is None


def test_all_successful_sources_are_silent(session: Session) -> None:
    """«все успешные → тишина»."""
    update_source_state(session, source_key="mintrud.gov.ru/docs", success_at=NOW - dt.timedelta(hours=1))
    session.commit()

    assert check_source_health(session, now=NOW) == []


def test_single_failed_attempt_is_not_enough(session: Session) -> None:
    """Спека: «>=2 подряд неудачных попыток» — одна неудача не должна поднимать тревогу."""
    record_source_failure(session, source_key="kremlin.ru/acts/news", attempt_at=NOW)
    session.commit()

    assert check_source_health(session, now=NOW) == []


def test_success_after_failures_resets_the_streak(session: Session) -> None:
    """`update_source_state` (успешный обход) должен сбрасывать `consecutive_failures` —
    иначе источник, оправившийся после сбоя, продолжал бы считаться больным."""
    record_source_failure(session, source_key="government.ru", attempt_at=NOW - dt.timedelta(hours=2))
    record_source_failure(session, source_key="government.ru", attempt_at=NOW - dt.timedelta(hours=1))
    update_source_state(session, source_key="government.ru", success_at=NOW)
    session.commit()

    assert check_source_health(session, now=NOW) == []


def test_stale_failures_older_than_24h_are_ignored(session: Session) -> None:
    """Источник, снятый из обхода (нет свежих попыток) — не должен висеть в алертах вечно."""
    source_key = "sfr.gov.ru/press_center/news"
    record_source_failure(session, source_key=source_key, attempt_at=NOW - dt.timedelta(hours=30))
    record_source_failure(session, source_key=source_key, attempt_at=NOW - dt.timedelta(hours=29))
    session.commit()

    assert check_source_health(session, now=NOW) == []


def test_format_alert_mentions_source_and_failure_count() -> None:
    from bot.health_alerts import UnhealthySource

    text = format_alert(
        [
            UnhealthySource(
                source_key="publication.pravo.gov.ru",
                consecutive_failures=4,
                last_success_at=dt.datetime(2026, 8, 29, 6, 0, tzinfo=dt.timezone.utc),
                last_attempt_at=NOW,
            )
        ]
    )

    assert "publication.pravo.gov.ru" in text
    assert "4" in text
    assert "29.08.2026" in text


def test_alert_dedup_suppresses_repeat_within_ttl(tmp_path) -> None:
    """«дедуп алерта (не спамить каждый digest)» — TTL 24ч."""
    from bot.health_alerts import UnhealthySource

    state_path = tmp_path / "health_alerts_state.json"
    unhealthy = [
        UnhealthySource(
            source_key="publication.pravo.gov.ru",
            consecutive_failures=3,
            last_success_at=None,
            last_attempt_at=NOW,
        )
    ]

    first = sources_due_for_alert(unhealthy, state_path=state_path, now=NOW)
    assert first == unhealthy

    # тот же самый прогон digest через час — источник всё ещё больной, но алерт уже слали
    second = sources_due_for_alert(unhealthy, state_path=state_path, now=NOW + dt.timedelta(hours=1))
    assert second == []


def test_alert_dedup_resends_after_ttl_expires(tmp_path) -> None:
    from bot.health_alerts import UnhealthySource

    state_path = tmp_path / "health_alerts_state.json"
    unhealthy = [
        UnhealthySource(
            source_key="publication.pravo.gov.ru",
            consecutive_failures=3,
            last_success_at=None,
            last_attempt_at=NOW,
        )
    ]

    sources_due_for_alert(unhealthy, state_path=state_path, now=NOW)
    later = sources_due_for_alert(unhealthy, state_path=state_path, now=NOW + dt.timedelta(hours=25))

    assert later == unhealthy


def test_alert_dedup_is_per_source(tmp_path) -> None:
    """Один больной источник не должен глушить алерт по другому, новому."""
    from bot.health_alerts import UnhealthySource

    state_path = tmp_path / "health_alerts_state.json"
    first_source = UnhealthySource(
        source_key="publication.pravo.gov.ru",
        consecutive_failures=3,
        last_success_at=None,
        last_attempt_at=NOW,
    )
    second_source = UnhealthySource(
        source_key="kremlin.ru/acts/news",
        consecutive_failures=2,
        last_success_at=None,
        last_attempt_at=NOW,
    )

    sources_due_for_alert([first_source], state_path=state_path, now=NOW)
    due = sources_due_for_alert(
        [first_source, second_source], state_path=state_path, now=NOW + dt.timedelta(minutes=30)
    )

    assert due == [second_source]


# --- Второй триггер: 0 новых сигналов + ни один ЖС-значимый источник не обходился
# успешно за 24ч (спека, раздел «Предполагаемый фикс», п.2) ---


def _make_signal_at(session: Session, created_at: dt.datetime) -> None:
    signal = create_signal(
        session,
        event_type=EventType.NEW_DOCUMENT,
        priority=Priority.HIGH,
        source_url="https://mintrud.gov.ru/docs/1",
        categories=[SignalCategory.VETERANS],
        region=REGION_RF,
        title="Приказ №1",
    )
    signal.created_at = created_at


def test_zero_signal_degradation_is_silent_on_a_never_run_parser(session: Session) -> None:
    # sources_state целиком пустая (парсер ни разу не запускался) — не с чем сравнивать.
    assert check_zero_signal_degradation(session, now=NOW) is False


def test_zero_signal_degradation_triggers_without_signals_or_successful_significant_sources(
    session: Session,
) -> None:
    record_source_failure(
        session, source_key="publication.pravo.gov.ru", attempt_at=NOW - dt.timedelta(hours=1)
    )
    session.commit()

    assert check_zero_signal_degradation(session, now=NOW) is True


def test_zero_signal_degradation_silent_when_a_recent_signal_exists(session: Session) -> None:
    _make_signal_at(session, NOW - dt.timedelta(hours=1))
    session.commit()

    assert check_zero_signal_degradation(session, now=NOW) is False


def test_zero_signal_degradation_silent_when_significant_source_succeeded_recently(
    session: Session,
) -> None:
    update_source_state(session, source_key="kremlin.ru/acts/news", success_at=NOW - dt.timedelta(hours=1))
    session.commit()

    assert check_zero_signal_degradation(session, now=NOW) is False


def test_zero_signal_degradation_silent_when_yandex_search_situation_succeeded_recently(
    session: Session,
) -> None:
    # per-ЖС записи имеют вид "yandex_search:<situation_id>" — префиксное совпадение.
    update_source_state(session, source_key="yandex_search:veterans", success_at=NOW - dt.timedelta(hours=1))
    session.commit()

    assert check_zero_signal_degradation(session, now=NOW) is False


def test_zero_signal_degradation_ignores_non_significant_source_success(session: Session) -> None:
    # mintrud.gov.ru/docs успешен, но не входит в ЖС-значимый список — не должен глушить триггер.
    update_source_state(session, source_key="mintrud.gov.ru/docs", success_at=NOW - dt.timedelta(hours=1))
    session.commit()

    assert check_zero_signal_degradation(session, now=NOW) is True


def test_zero_signal_degradation_ignores_stale_successes_older_than_24h(session: Session) -> None:
    update_source_state(session, source_key="government.ru/docs", success_at=NOW - dt.timedelta(hours=30))
    session.commit()

    assert check_zero_signal_degradation(session, now=NOW) is True


def test_format_zero_signal_alert_mentions_key_sources() -> None:
    text = format_zero_signal_alert()

    assert "⚠️" in text
    assert "publication.pravo.gov.ru" in text
    assert "kremlin.ru/acts/news" in text
    assert "government.ru/docs" in text
    assert "yandex_search" in text


def test_zero_signal_alert_dedup_suppresses_repeat_within_ttl(tmp_path) -> None:
    state_path = tmp_path / "health_alerts_state.json"

    first = zero_signal_alert_due(state_path=state_path, now=NOW)
    second = zero_signal_alert_due(state_path=state_path, now=NOW + dt.timedelta(hours=1))

    assert first is True
    assert second is False


def test_zero_signal_alert_dedup_resends_after_ttl_expires(tmp_path) -> None:
    state_path = tmp_path / "health_alerts_state.json"

    zero_signal_alert_due(state_path=state_path, now=NOW)
    later = zero_signal_alert_due(state_path=state_path, now=NOW + dt.timedelta(hours=25))

    assert later is True


def test_zero_signal_alert_dedup_survives_per_source_cleanup(tmp_path) -> None:
    """`sources_due_for_alert` подчищает state-файл от источников, переставших быть
    больными (см. тест выше) — запись второго триггера (ключ с префиксом "__") не должна
    попасть под эту чистку, иначе TTL-дедуп по нему сломается на первом же digest,
    где были ещё и per-источник алерты."""
    from bot.health_alerts import UnhealthySource

    state_path = tmp_path / "health_alerts_state.json"
    zero_signal_alert_due(state_path=state_path, now=NOW)

    unhealthy = [
        UnhealthySource(
            source_key="publication.pravo.gov.ru",
            consecutive_failures=3,
            last_success_at=None,
            last_attempt_at=NOW,
        )
    ]
    sources_due_for_alert(unhealthy, state_path=state_path, now=NOW + dt.timedelta(minutes=10))

    # тот же самый прогон полчаса спустя — второй триггер всё ещё должен молчать по TTL
    still_deduped = zero_signal_alert_due(state_path=state_path, now=NOW + dt.timedelta(minutes=30))
    assert still_deduped is False
