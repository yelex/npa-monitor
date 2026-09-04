"""Тесты parser/signals.py, PLAN.md Фаза 4."""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from db.enums import REGION_RF, REGION_UNDEFINED, EventType, Priority, SignalCategory, SignalStatus
from db.models import Signal
from db.session import init_db, make_engine, make_session_factory
from parser.classifier import ClassificationResult
from parser.models import Publication
from parser.signals import build_signal, is_review, is_review_aggregate


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


def _review_publication(*, title: str, url: str) -> Publication:
    return Publication(source_key="consultant.ru", title=title, url=url, published_at=None)


def test_is_review_aggregate_true_for_law_review_url_even_when_event_type_not_review() -> None:
    # Инцидент #296: detect_event_type дал AMENDMENT (не REVIEW), is_review сам по себе
    # не срабатывает — is_review_aggregate ловит по url `/law/review/`.
    classification = ClassificationResult(
        is_relevant=True,
        categories=(SignalCategory.VETERANS,),
        event_type=EventType.AMENDMENT,
        region=REGION_UNDEFINED,
        priority=Priority.MEDIUM,
    )
    publication = _review_publication(
        title="Новое в московском законодательстве. Выпуск за 3 сентября 2026 года"
        " \\ Обзоры законодательства \\ КонсультантПлюс",
        url="https://www.consultant.ru/law/review/reg/md2026-09-03.html",
    )

    assert is_review_aggregate(publication, classification) is True


def test_is_review_aggregate_true_for_review_title_marker_without_url_pattern() -> None:
    classification = ClassificationResult(
        is_relevant=True,
        categories=(SignalCategory.VETERANS,),
        event_type=EventType.AMENDMENT,
        region=REGION_UNDEFINED,
        priority=Priority.MEDIUM,
    )
    publication = _review_publication(
        title="Обзор законодательства для бухгалтера за неделю",
        url="https://www.consultant.ru/some/other/path.html",
    )

    assert is_review_aggregate(publication, classification) is True


def test_is_review_aggregate_false_for_ordinary_publication_with_event_marker() -> None:
    classification = ClassificationResult(
        is_relevant=True,
        categories=(SignalCategory.VETERANS,),
        event_type=EventType.NEW_DOCUMENT,
        region=REGION_RF,
        priority=Priority.HIGH,
    )
    publication = _review_publication(
        title="постановление о мерах поддержки ветеранов боевых действий принято",
        url="https://www.consultant.ru/document/cons_doc_LAW_123456/",
    )

    assert is_review_aggregate(publication, classification) is False
