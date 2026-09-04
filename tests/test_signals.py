"""Тесты parser/signals.py, PLAN.md Фаза 4."""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from db.enums import REGION_RF, REGION_UNDEFINED, EventType, Priority, SignalCategory, SignalStatus
from db.models import Signal
from db.session import init_db, make_engine, make_session_factory
from parser.classifier import ClassificationResult
from parser.models import Publication
from parser.signals import build_signal, is_review


@pytest.fixture
def session(tmp_path) -> Session:
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        yield session


def _publication() -> Publication:
    return Publication(
        source_key="sfr.gov.ru/press_center/news",
        title="постановление ветеран боевых действий выплата новый",
        url="https://sfr.gov.ru/press_center/news/1",
        published_at=None,
    )


def test_build_signal_creates_signal_for_relevant_publication(session: Session) -> None:
    classification = ClassificationResult(
        is_relevant=True,
        categories=(SignalCategory.VETERANS,),
        event_type=EventType.NEW_DOCUMENT,
        region=REGION_RF,
        priority=Priority.HIGH,
    )

    signal = build_signal(session, _publication(), classification)
    session.commit()

    assert signal is not None
    assert signal.status == SignalStatus.NEW
    assert signal.event_type == EventType.NEW_DOCUMENT
    assert signal.priority == Priority.HIGH
    assert signal.region == REGION_RF
    assert signal.source_url == "https://sfr.gov.ru/press_center/news/1"
    assert signal.title == "постановление ветеран боевых действий выплата новый"
    assert [c.category for c in signal.categories] == [SignalCategory.VETERANS]


def test_build_signal_returns_none_for_irrelevant_publication(session: Session) -> None:
    classification = ClassificationResult(
        is_relevant=False,
        categories=(),
        event_type=EventType.REVIEW,
        region=REGION_UNDEFINED,
        priority=Priority.LOW,
    )

    signal = build_signal(session, _publication(), classification)

    assert signal is None
    assert session.query(Signal).count() == 0


def test_is_review_true_only_when_relevant_and_review_event_type() -> None:
    relevant_review = ClassificationResult(
        is_relevant=True,
        categories=(SignalCategory.VETERANS,),
        event_type=EventType.REVIEW,
        region=REGION_RF,
        priority=Priority.MEDIUM,
    )
    relevant_with_marker = ClassificationResult(
        is_relevant=True,
        categories=(SignalCategory.VETERANS,),
        event_type=EventType.NEW_DOCUMENT,
        region=REGION_RF,
        priority=Priority.HIGH,
    )
    irrelevant_review = ClassificationResult(
        is_relevant=False,
        categories=(),
        event_type=EventType.REVIEW,
        region=REGION_UNDEFINED,
        priority=Priority.LOW,
    )

    assert is_review(relevant_review) is True
    assert is_review(relevant_with_marker) is False
    assert is_review(irrelevant_review) is False
