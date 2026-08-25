"""Тесты scripts/cleanup_signals.py, PLAN.md Фаза 9 п.7,
docs/SPEC_retroactive_signals_cleanup.md."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from db.enums import REGION_RF, REGION_UNDEFINED, EventType, Priority, SignalCategory, SignalStatus
from db.models import Signal
from db.service import create_signal, transition_status
from db.session import init_db, make_engine, make_session_factory
from parser.classifier import Classifier
from scripts.cleanup_signals import run_cleanup

NOW = dt.datetime(2026, 8, 24, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def session(tmp_path) -> Session:
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        yield session


@pytest.fixture
def classifier() -> Classifier:
    return Classifier.load()


def _make_signal(
    session: Session,
    *,
    title: str,
    source_url: str,
    priority: Priority = Priority.MEDIUM,
    region: str = REGION_RF,
    status: SignalStatus = SignalStatus.NEW,
    created_at: dt.datetime = NOW,
) -> Signal:
    signal = create_signal(
        session,
        event_type=EventType.NEW_DOCUMENT,
        priority=priority,
        source_url=source_url,
        categories=[SignalCategory.VETERANS],
        region=region,
        title=title,
    )
    signal.created_at = created_at
    if status != SignalStatus.NEW:
        transition_status(session, signal, status, changed_by="test")
    session.commit()
    return signal


def test_step_a_rejects_excluded_url_pattern(session: Session, classifier: Classifier) -> None:
    signal = _make_signal(
        session,
        title="ветеран боевых действий выплата",
        source_url="https://sfr.gov.ru/branches/77/info/~2026/08/20/12345?info_category=1",
    )

    report = run_cleanup(session, classifier, apply=True)
    session.refresh(signal)

    assert len(report.excluded_by_url) == 1
    assert report.excluded_by_url[0].signal_id == signal.id
    assert signal.status == SignalStatus.REJECTED


def test_step_a_dry_run_does_not_change_status(session: Session, classifier: Classifier) -> None:
    signal = _make_signal(
        session,
        title="ветеран боевых действий выплата",
        source_url="https://sfr.gov.ru/branches/77/info/~2026/08/20/12345",
    )

    report = run_cleanup(session, classifier, apply=False)
    session.refresh(signal)

    assert len(report.excluded_by_url) == 1
    assert signal.status == SignalStatus.NEW  # отчёт не применяет изменения


def test_step_b1_dedups_by_canonical_url(session: Session, classifier: Classifier) -> None:
    keeper = _make_signal(
        session,
        title="постановление ветеран боевых действий выплата",
        source_url="https://publication.pravo.gov.ru/document/1?index=1",
        created_at=NOW,
    )
    dupe = _make_signal(
        session,
        title="постановление ветеран боевых действий выплата",
        source_url="http://www.publication.pravo.gov.ru/document/1?index=2",
        created_at=NOW + dt.timedelta(hours=1),
    )

    report = run_cleanup(session, classifier, apply=True)
    session.refresh(keeper)
    session.refresh(dupe)

    assert len(report.duplicates_by_url) == 1
    assert report.duplicates_by_url[0].signal_id == dupe.id
    assert keeper.status == SignalStatus.NEW
    assert dupe.status == SignalStatus.REJECTED


def test_step_b2_dedups_by_title_content(session: Session, classifier: Classifier) -> None:
    keeper = _make_signal(
        session,
        title="Ветераны боевых действий столичного региона получат новую выплату",
        source_url="https://zao.mos.ru/news/1",
        created_at=NOW,
    )
    dupe = _make_signal(
        session,
        title="Ветераны боевых действий столичного региона получат новую выплату",
        source_url="https://svao.mos.ru/news/2",
        created_at=NOW + dt.timedelta(hours=2),
    )

    report = run_cleanup(session, classifier, apply=True)
    session.refresh(keeper)
    session.refresh(dupe)

    assert len(report.duplicates_by_title) == 1
    assert report.duplicates_by_title[0].signal_id == dupe.id
    assert keeper.status == SignalStatus.NEW
    assert dupe.status == SignalStatus.REJECTED


def test_step_c_raises_stale_low_priority(session: Session, classifier: Classifier) -> None:
    # Фаза 9 п.5: тот же текст, что и регрессионный тест на сигнал [30] дампа —
    # текущий классификатор даёт medium, сигнал сохранён с устаревшим low.
    signal = _make_signal(
        session,
        title="Евгений Солнцев: в Оренбуржье ввели новую меру поддержки участникам СВО",
        source_url="https://rg.ru/2026/08/10/x.html",
        priority=Priority.LOW,
        region=REGION_UNDEFINED,
    )

    report = run_cleanup(session, classifier, apply=True)
    session.refresh(signal)

    assert len(report.priority_changes) == 1
    change = report.priority_changes[0]
    assert change.signal_id == signal.id
    assert change.old_priority == "low"
    assert change.new_priority == "medium"
    assert signal.priority == Priority.MEDIUM


def test_step_c_no_change_when_priority_already_matches(session: Session, classifier: Classifier) -> None:
    signal = _make_signal(
        session,
        title="постановление ветеран боевых действий выплата новый",
        source_url="https://kremlin.ru/acts/1",
        priority=Priority.HIGH,
        region=REGION_RF,
    )

    report = run_cleanup(session, classifier, apply=True)
    session.refresh(signal)

    assert report.priority_changes == []
    assert signal.priority == Priority.HIGH


def test_step_d_flags_irrelevant_signal_without_changing_status(
    session: Session, classifier: Classifier
) -> None:
    signal = _make_signal(
        session,
        title="Отчёт о рыболовстве и пятилетнем плане экономического развития",
        source_url="https://rg.ru/2026/08/10/fishing.html",
    )

    report = run_cleanup(session, classifier, apply=True)
    session.refresh(signal)

    assert len(report.relevance_flags) == 1
    assert report.relevance_flags[0].signal_id == signal.id
    assert signal.status == SignalStatus.NEW  # шаг D никогда не меняет статус


def test_finalized_statuses_are_never_touched(session: Session, classifier: Classifier) -> None:
    signal = _make_signal(
        session,
        title="ветеран боевых действий выплата",
        source_url="https://sfr.gov.ru/branches/77/info/~2026/08/20/1",
        status=SignalStatus.IN_PROGRESS,
    )

    report = run_cleanup(session, classifier, apply=True)
    session.refresh(signal)

    assert report.excluded_by_url == []
    assert signal.status == SignalStatus.IN_PROGRESS
