"""Тесты scripts/audit_rejected_signals.py, PLAN.md Фаза 9 п.7,
docs/SPEC_retroactive_signals_cleanup.md, раздел 8."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from db.enums import REGION_RF, EventType, Priority, RejectionReason, SignalCategory, SignalStatus
from db.service import create_signal, transition_status
from db.session import init_db, make_engine, make_session_factory
from parser.classifier import Classifier
from scripts.audit_rejected_signals import audit

NOW = dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.timezone.utc)


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


def _rejected_signal(
    session: Session,
    *,
    title: str,
    source_url: str,
    rejection_reason: RejectionReason,
    created_at: dt.datetime = NOW,
):
    signal = create_signal(
        session,
        event_type=EventType.NEW_DOCUMENT,
        priority=Priority.MEDIUM,
        source_url=source_url,
        categories=[SignalCategory.VETERANS],
        region=REGION_RF,
        title=title,
    )
    signal.created_at = created_at
    transition_status(
        session,
        signal,
        SignalStatus.REJECTED,
        changed_by="expert1",
        rejection_reason=rejection_reason,
    )
    session.commit()
    return signal


def test_catches_excluded_url_pattern(session: Session, classifier: Classifier) -> None:
    signal = _rejected_signal(
        session,
        title="ветеран боевых действий выплата",
        source_url="https://sfr.gov.ru/branches/77/info/~2026/08/20/1",
        rejection_reason=RejectionReason.NOT_TARGET_CATEGORY,
    )

    caught = audit([signal], classifier)

    assert caught[signal.id].startswith("A:")


def test_catches_url_duplicate(session: Session, classifier: Classifier) -> None:
    keeper = _rejected_signal(
        session,
        title="постановление ветеран боевых действий выплата",
        source_url="https://publication.pravo.gov.ru/document/1?index=1",
        rejection_reason=RejectionReason.DUPLICATE,
        created_at=NOW,
    )
    dupe = _rejected_signal(
        session,
        title="постановление ветеран боевых действий выплата",
        source_url="http://www.publication.pravo.gov.ru/document/1?index=2",
        rejection_reason=RejectionReason.DUPLICATE,
        created_at=NOW + dt.timedelta(hours=1),
    )

    caught = audit([keeper, dupe], classifier)

    assert keeper.id not in caught
    assert caught[dupe.id].startswith("B1:")


def test_catches_irrelevant_content(session: Session, classifier: Classifier) -> None:
    signal = _rejected_signal(
        session,
        title="Отчёт о рыболовстве и пятилетнем плане экономического развития",
        source_url="https://rg.ru/2026/08/10/fishing.html",
        rejection_reason=RejectionReason.NOT_TARGET_CATEGORY,
    )

    caught = audit([signal], classifier)

    assert caught[signal.id].startswith("D:")


def test_does_not_flag_signal_without_matching_pattern(session: Session, classifier: Classifier) -> None:
    signal = _rejected_signal(
        session,
        title="постановление ветеран боевых действий новая выплата",
        source_url="https://kremlin.ru/acts/unique-1",
        rejection_reason=RejectionReason.OTHER,
    )

    caught = audit([signal], classifier)

    assert signal.id not in caught


def test_audit_never_writes_to_db(session: Session, classifier: Classifier) -> None:
    signal = _rejected_signal(
        session,
        title="ветеран боевых действий выплата",
        source_url="https://sfr.gov.ru/branches/77/info/~2026/08/20/1",
        rejection_reason=RejectionReason.NOT_TARGET_CATEGORY,
    )
    original_status = signal.status
    original_reason = signal.rejection_reason

    audit([signal], classifier)
    session.refresh(signal)

    assert signal.status == original_status
    assert signal.rejection_reason == original_reason
