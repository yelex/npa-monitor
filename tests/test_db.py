from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.enums import EventType, Priority, Region, RejectionReason, SignalCategory, SignalStatus
from db.models import DocumentSeen, Signal, SourceState
from db.service import (
    InvalidStatusTransition,
    create_signal,
    register_document_seen,
    transition_status,
    update_source_state,
)
from db.session import init_db, make_engine, make_session_factory


@pytest.fixture
def session(tmp_path) -> Session:
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        yield session


def _make_signal(session: Session, **overrides) -> Signal:
    kwargs = dict(
        event_type=EventType.NEW_DOCUMENT,
        priority=Priority.HIGH,
        source_url="https://mintrud.gov.ru/docs/1",
        categories=[SignalCategory.VETERANS],
        region=Region.RF,
        title="Приказ №1",
    )
    kwargs.update(overrides)
    signal = create_signal(session, **kwargs)
    session.commit()
    return signal


def test_create_signal_defaults_to_new_status_with_history(session: Session):
    signal = _make_signal(session)

    assert signal.id is not None
    assert signal.status == SignalStatus.NEW
    assert [c.category for c in signal.categories] == [SignalCategory.VETERANS]
    assert len(signal.history) == 1
    assert signal.history[0].from_status is None
    assert signal.history[0].to_status == SignalStatus.NEW


def test_multiple_categories_are_stored(session: Session):
    signal = _make_signal(
        session, categories=[SignalCategory.VETERANS, SignalCategory.SVO]
    )

    stored = {c.category for c in signal.categories}
    assert stored == {SignalCategory.VETERANS, SignalCategory.SVO}


def test_valid_transition_chain_new_to_completed(session: Session):
    signal = _make_signal(session)

    transition_status(session, signal, SignalStatus.IN_PROGRESS, changed_by="expert1")
    transition_status(session, signal, SignalStatus.SENT_TO_AGENT, changed_by="expert1")
    transition_status(session, signal, SignalStatus.COMPLETED, changed_by="expert1")
    session.commit()

    assert signal.status == SignalStatus.COMPLETED
    to_statuses = [h.to_status for h in signal.history]
    assert to_statuses == [
        SignalStatus.NEW,
        SignalStatus.IN_PROGRESS,
        SignalStatus.SENT_TO_AGENT,
        SignalStatus.COMPLETED,
    ]


def test_postponed_returns_to_in_progress(session: Session):
    signal = _make_signal(session)
    transition_status(session, signal, SignalStatus.IN_PROGRESS)
    transition_status(session, signal, SignalStatus.POSTPONED)
    transition_status(session, signal, SignalStatus.IN_PROGRESS)
    session.commit()

    assert signal.status == SignalStatus.IN_PROGRESS


def test_invalid_transition_new_to_completed_raises(session: Session):
    signal = _make_signal(session)

    with pytest.raises(InvalidStatusTransition):
        transition_status(session, signal, SignalStatus.COMPLETED)


def test_invalid_transition_from_terminal_completed_raises(session: Session):
    signal = _make_signal(session)
    transition_status(session, signal, SignalStatus.IN_PROGRESS)
    transition_status(session, signal, SignalStatus.SENT_TO_AGENT)
    transition_status(session, signal, SignalStatus.COMPLETED)
    session.commit()

    with pytest.raises(InvalidStatusTransition):
        transition_status(session, signal, SignalStatus.NEW)


def test_reject_requires_reason(session: Session):
    signal = _make_signal(session)
    transition_status(session, signal, SignalStatus.IN_PROGRESS)

    with pytest.raises(ValueError):
        transition_status(session, signal, SignalStatus.REJECTED)


def test_reject_then_reopen_resets_reason(session: Session):
    signal = _make_signal(session)
    transition_status(session, signal, SignalStatus.IN_PROGRESS)
    transition_status(
        session,
        signal,
        SignalStatus.REJECTED,
        rejection_reason=RejectionReason.DUPLICATE,
    )
    session.commit()
    assert signal.status == SignalStatus.REJECTED
    assert signal.rejection_reason == RejectionReason.DUPLICATE

    transition_status(session, signal, SignalStatus.NEW, changed_by="expert1", reason="reopen")
    session.commit()

    assert signal.status == SignalStatus.NEW
    assert signal.rejection_reason is None
    assert signal.rejection_comment is None


def test_documents_seen_dedup_via_service(session: Session):
    _, created_first = register_document_seen(
        session, source_key="mintrud.gov.ru/docs", doc_url="https://mintrud.gov.ru/docs/1"
    )
    session.commit()
    _, created_second = register_document_seen(
        session, source_key="mintrud.gov.ru/docs", doc_url="https://mintrud.gov.ru/docs/1"
    )
    session.commit()

    assert created_first is True
    assert created_second is False
    assert session.query(DocumentSeen).count() == 1


def test_documents_seen_unique_constraint_at_db_level(session: Session):
    session.add(DocumentSeen(source_key="src", doc_url="https://example.gov.ru/doc/1"))
    session.commit()

    session.add(DocumentSeen(source_key="src", doc_url="https://example.gov.ru/doc/1"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_source_state_upsert(session: Session):
    now = dt.datetime.now(dt.timezone.utc)
    update_source_state(session, source_key="mintrud.gov.ru/docs", success_at=now)
    session.commit()

    later = now + dt.timedelta(days=1)
    update_source_state(
        session,
        source_key="mintrud.gov.ru/docs",
        success_at=later,
        last_seen_publication_date=later.date(),
    )
    session.commit()

    assert session.query(SourceState).count() == 1
    state = session.get(SourceState, "mintrud.gov.ru/docs")
    # SQLite не хранит tzinfo — сравниваем naive-версию (значения были в UTC).
    assert state.last_success_at == later.replace(tzinfo=None)
    assert state.last_seen_publication_date == later.date()
