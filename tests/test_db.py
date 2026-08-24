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
    link_document_to_signal,
    recent_documents_with_titles,
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


def test_new_can_be_postponed_directly(session: Session):
    """AGENTS.md раздел 6, таблица переходов: Новый -> Отложен (↩️ Позже)."""
    signal = _make_signal(session)

    transition_status(session, signal, SignalStatus.POSTPONED)
    session.commit()

    assert signal.status == SignalStatus.POSTPONED


def test_postponed_can_be_rejected(session: Session):
    """AGENTS.md раздел 6, таблица переходов: Отложен -> Отклонён (❌ Отклонить)."""
    signal = _make_signal(session)
    transition_status(session, signal, SignalStatus.POSTPONED)

    transition_status(
        session, signal, SignalStatus.REJECTED, rejection_reason=RejectionReason.OTHER
    )
    session.commit()

    assert signal.status == SignalStatus.REJECTED


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


def test_register_document_seen_stores_title(session: Session):
    document, _ = register_document_seen(
        session, source_key="src", doc_url="https://example.gov.ru/doc/1", title="Заголовок"
    )
    session.commit()

    assert document.title == "Заголовок"


def test_recent_documents_with_titles_filters_by_window_and_title_presence(session: Session):
    now = dt.datetime.now(dt.timezone.utc)
    register_document_seen(session, source_key="src", doc_url="https://example.gov.ru/1", title="Свежий")
    register_document_seen(session, source_key="src", doc_url="https://example.gov.ru/2", title=None)
    session.commit()
    old_doc = DocumentSeen(
        source_key="src",
        doc_url="https://example.gov.ru/3",
        title="Старый",
        first_seen_at=now - dt.timedelta(days=30),
    )
    session.add(old_doc)
    session.commit()

    recent = recent_documents_with_titles(session, since=now - dt.timedelta(days=5))

    titles = {doc.title for doc in recent}
    assert titles == {"Свежий"}  # без title и вне окна — не попадают в кандидаты


def test_recent_documents_with_titles_excludes_given_id(session: Session):
    document, _ = register_document_seen(
        session, source_key="src", doc_url="https://example.gov.ru/1", title="Заголовок"
    )
    session.commit()

    recent = recent_documents_with_titles(
        session, since=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1), exclude_id=document.id
    )

    assert recent == []


def test_link_document_to_signal_sets_signal_id(session: Session):
    signal = _make_signal(session)
    document, _ = register_document_seen(
        session, source_key="src", doc_url="https://example.gov.ru/1", title="Заголовок"
    )
    session.commit()

    link_document_to_signal(session, document, signal_id=signal.id)
    session.commit()

    assert document.signal_id == signal.id


def test_documents_seen_unique_constraint_at_db_level(session: Session):
    session.add(DocumentSeen(source_key="src", doc_url="https://example.gov.ru/doc/1"))
    session.commit()

    session.add(DocumentSeen(source_key="src", doc_url="https://example.gov.ru/doc/1"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_init_db_adds_title_column_to_pre_existing_documents_seen_table(tmp_path):
    # docs/SPEC_content_dedup.md, раздел 3.1: на уже развёрнутой боевой БД (без Alembic)
    # documents_seen существует без колонки title — init_db должен добавить её и не
    # потерять уже накопленные строки, а не упасть на create_all (который не меняет
    # существующие таблицы).
    from sqlalchemy import text as sql_text

    engine = make_engine(tmp_path / "legacy.db")
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "CREATE TABLE documents_seen ("
                "id INTEGER PRIMARY KEY, source_key VARCHAR(128), doc_url TEXT, "
                "first_seen_at DATETIME, signal_id INTEGER, "
                "CONSTRAINT uq_documents_seen_doc_url UNIQUE (doc_url))"
            )
        )
        conn.execute(
            sql_text(
                "INSERT INTO documents_seen (source_key, doc_url, first_seen_at) "
                "VALUES ('src', 'https://example.gov.ru/legacy', '2026-01-01 00:00:00')"
            )
        )

    init_db(engine)

    with engine.begin() as conn:
        columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(documents_seen)")}
        assert "title" in columns
        row = conn.execute(
            sql_text("SELECT doc_url, title FROM documents_seen WHERE doc_url = 'https://example.gov.ru/legacy'")
        ).one()
        assert row.doc_url == "https://example.gov.ru/legacy"
        assert row.title is None  # старые строки — без заголовка, не участвуют в дедупе по содержанию


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
    # db.types.UTCDateTime гарантирует tz-aware datetime на чтении даже на SQLite.
    assert state.last_success_at == later
    assert state.last_seen_publication_date == later.date()
