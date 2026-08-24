"""Тесты parser/llm_priority.py, PLAN.md Фаза 11, docs/SPEC_llm_priority.md."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from sqlalchemy.orm import Session

from db.enums import EventType, Priority, Region, SignalCategory
from db.models import Signal
from db.service import create_signal
from db.session import init_db, make_engine, make_session_factory
from parser.llm import LLMError
from parser.llm_priority import (
    apply_refinements,
    chunk_signal_ids,
    log_refinement,
    refine_priorities_batch,
)
from parser.models import Publication
from parser.orchestrator import SourceSpec, run_all


@pytest.fixture
def session(tmp_path) -> Session:
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        yield session


class _StubLLMClient:
    """Отдаёт заготовленные ответы по очереди — по одному на вызов `complete()`."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    def complete(self, prompt: str) -> str:
        self.calls.append(prompt)
        if not self._responses:
            raise AssertionError("LLM вызван больше раз, чем предоставлено ответов")
        return self._responses.pop(0)


class _AlwaysFailingLLMClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str) -> str:
        self.calls += 1
        raise LLMError("недоступен")


def _make_signal(
    session: Session,
    *,
    title: str,
    priority: Priority,
    region: Region = Region.MOSCOW,
    categories: tuple[SignalCategory, ...] = (SignalCategory.VETERANS,),
    event_type: EventType = EventType.AMENDMENT,
) -> Signal:
    return create_signal(
        session,
        event_type=event_type,
        priority=priority,
        source_url="https://mos.ru/x",
        categories=list(categories),
        region=region,
        title=title,
    )


# --- chunk_signal_ids ---


def test_chunk_signal_ids_splits_into_default_size_chunks() -> None:
    ids = list(range(1, 45))

    chunks = chunk_signal_ids(ids)

    assert [len(c) for c in chunks] == [20, 20, 4]
    assert [sid for chunk in chunks for sid in chunk] == ids


def test_chunk_signal_ids_empty_input() -> None:
    assert chunk_signal_ids([]) == []


# --- refine_priorities_batch: батч-парсер ---


def test_refine_priorities_batch_applies_one_level_shift_from_valid_json(session: Session) -> None:
    signal = _make_signal(session, title="Увеличена выплата ветеранам до 20000 рублей", priority=Priority.MEDIUM)
    llm = _StubLLMClient(
        [json.dumps([{"id": signal.id, "priority": "high", "reason": "конкретная сумма выплаты"}])]
    )

    [refinement] = refine_priorities_batch(session, [signal.id], llm)

    assert refinement.regex_priority == Priority.MEDIUM
    assert refinement.llm_priority == Priority.HIGH
    assert refinement.final_priority == Priority.HIGH
    assert refinement.source == "llm_adjusted"
    assert refinement.discrepancy is False
    assert len(llm.calls) == 1


def test_refine_priorities_batch_retries_once_on_broken_json_then_falls_back_to_regex(
    session: Session,
) -> None:
    signal = _make_signal(session, title="Обзор мер поддержки ветеранов", priority=Priority.LOW)
    llm = _StubLLMClient(["это не json совсем", "и это тоже не json"])

    [refinement] = refine_priorities_batch(session, [signal.id], llm)

    assert len(llm.calls) == 2  # один ретрай батча, дальше fallback
    assert refinement.source == "regex"
    assert refinement.final_priority == Priority.LOW
    assert refinement.llm_priority is None


def test_refine_priorities_batch_partial_response_falls_back_per_signal(session: Session) -> None:
    malformed = _make_signal(session, title="Сигнал с некорректным элементом ответа", priority=Priority.LOW)
    missing = _make_signal(session, title="Сигнал, отсутствующий в ответе LLM", priority=Priority.LOW)
    shifted = _make_signal(session, title="Изменение суммы выплаты ветеранам", priority=Priority.MEDIUM)

    response = json.dumps(
        [
            {"id": malformed.id, "priority": "extreme"},  # невалидное значение priority
            {"id": shifted.id, "priority": "high", "reason": "конкретная сумма"},
        ]
    )
    llm = _StubLLMClient([response])

    refinements = {
        r.signal_id: r for r in refine_priorities_batch(session, [malformed.id, missing.id, shifted.id], llm)
    }

    assert len(llm.calls) == 1  # весь JSON валиден, ретрая не требуется
    assert refinements[malformed.id].source == "regex"
    assert refinements[malformed.id].llm_priority is None
    assert refinements[missing.id].source == "regex"
    assert refinements[missing.id].llm_priority is None
    assert refinements[shifted.id].source == "llm_adjusted"
    assert refinements[shifted.id].final_priority == Priority.HIGH


# --- Комбинирование: сдвиг максимум на один уровень ---


def test_refine_priorities_batch_skips_low_to_high_jump_as_discrepancy(session: Session) -> None:
    signal = _make_signal(session, title="Информационная статья без конкретного акта", priority=Priority.LOW)
    llm = _StubLLMClient([json.dumps([{"id": signal.id, "priority": "high", "reason": "выглядит важно"}])])

    [refinement] = refine_priorities_batch(session, [signal.id], llm)

    assert refinement.llm_priority == Priority.HIGH
    assert refinement.final_priority == Priority.LOW  # regex побеждает, скачок не применяется
    assert refinement.source == "regex"
    assert refinement.discrepancy is True


def test_refine_priorities_batch_medium_to_low_is_one_level_shift(session: Session) -> None:
    signal = _make_signal(session, title="Формальное упоминание меры без сути", priority=Priority.MEDIUM)
    llm = _StubLLMClient([json.dumps([{"id": signal.id, "priority": "low", "reason": "нет сути события"}])])

    [refinement] = refine_priorities_batch(session, [signal.id], llm)

    assert refinement.final_priority == Priority.LOW
    assert refinement.source == "llm_adjusted"
    assert refinement.discrepancy is False


# --- fallback при LLMError ---


def test_refine_priorities_batch_falls_back_when_llm_raises_every_attempt(session: Session) -> None:
    signal = _make_signal(session, title="Изменение порядка получения выплаты", priority=Priority.MEDIUM)
    llm = _AlwaysFailingLLMClient()

    [refinement] = refine_priorities_batch(session, [signal.id], llm)

    assert llm.calls == 2
    assert refinement.source == "regex"
    assert refinement.final_priority == Priority.MEDIUM


def test_refine_priorities_batch_without_llm_client_returns_regex_only_and_does_not_call_anything(
    session: Session,
) -> None:
    signal = _make_signal(session, title="Что-то среднее по важности", priority=Priority.MEDIUM)

    [refinement] = refine_priorities_batch(session, [signal.id], None)

    assert refinement.source == "regex"
    assert refinement.llm_priority is None


# --- Скоуп: HIGH не пересматривается ---


def test_refine_priorities_batch_skips_high_priority_signals(session: Session) -> None:
    signal = _make_signal(session, title="Уже высокий приоритет", priority=Priority.HIGH)
    llm = _StubLLMClient([])

    refinements = refine_priorities_batch(session, [signal.id], llm)

    assert refinements == []
    assert llm.calls == []


# --- apply_refinements ---


def test_apply_refinements_writes_only_llm_adjusted_changes(session: Session) -> None:
    signal = _make_signal(session, title="Изменение суммы выплаты", priority=Priority.MEDIUM)
    llm = _StubLLMClient([json.dumps([{"id": signal.id, "priority": "high", "reason": "..."}])])

    refinements = refine_priorities_batch(session, [signal.id], llm)
    applied = apply_refinements(session, refinements)
    session.commit()

    assert len(applied) == 1
    session.refresh(signal)
    assert signal.priority == Priority.HIGH


def test_apply_refinements_does_not_write_when_source_is_regex(session: Session) -> None:
    signal = _make_signal(session, title="Обзор без конкретики", priority=Priority.LOW)

    refinements = refine_priorities_batch(session, [signal.id], None)
    applied = apply_refinements(session, refinements)
    session.commit()

    assert applied == []
    session.refresh(signal)
    assert signal.priority == Priority.LOW


def test_log_refinement_does_not_raise(caplog: pytest.LogCaptureFixture, session: Session) -> None:
    signal = _make_signal(session, title="Изменение суммы выплаты", priority=Priority.MEDIUM)
    llm = _StubLLMClient([json.dumps([{"id": signal.id, "priority": "high", "reason": "..."}])])
    [refinement] = refine_priorities_batch(session, [signal.id], llm)

    with caplog.at_level("INFO"):
        log_refinement(refinement, applied=False)

    assert "log-only" in caplog.text


# --- Интеграция с orchestrator.run_all ---


NOW = dt.datetime(2026, 8, 24, 6, 0, tzinfo=dt.timezone.utc)


def _pub(url: str, title: str) -> Publication:
    return Publication(source_key="sfr.gov.ru/press_center/news", title=title, url=url, published_at=NOW)


def test_run_all_with_llm_client_none_leaves_priority_unchanged(session: Session) -> None:
    # regex-приоритет: маркер документа есть ("постановление"), слов приоритета нет,
    # регион федеральный (sfr.gov.ru) -> MEDIUM.
    publications = [_pub("https://sfr.gov.ru/n/100", "постановление ветеран боевых действий выплата")]
    spec = SourceSpec("sfr.gov.ru/press_center/news", lambda page=1: publications if page == 1 else [])

    results = run_all(session, specs=[spec], now=NOW, llm_client=None)

    assert results[0].medium_low_signal_ids  # сигнал собран для LLM-прохода
    signal = session.query(Signal).one()
    assert signal.priority == Priority.MEDIUM  # llm_client=None -> детерминированный regex-путь


def test_run_all_log_only_mode_does_not_change_priority_in_db(session: Session) -> None:
    publications = [_pub("https://sfr.gov.ru/n/101", "постановление ветеран боевых действий выплата")]
    spec = SourceSpec("sfr.gov.ru/press_center/news", lambda page=1: publications if page == 1 else [])

    class _DynamicLLMClient:
        # Отвечает "high" для реально созданного сигналом id — узнаём его через сессию,
        # т.к. заранее (до прогона run_all) id ещё не присвоен.
        def complete(self, prompt: str) -> str:
            signal = session.query(Signal).one()
            return json.dumps([{"id": signal.id, "priority": "high", "reason": "сумма выплаты"}])

    results = run_all(session, specs=[spec], now=NOW, llm_client=_DynamicLLMClient(), llm_priority_apply=False)

    assert results[0].medium_low_signal_ids
    signal = session.query(Signal).one()
    assert signal.priority == Priority.MEDIUM  # log-only: LLM предложил бы HIGH, но БД не менялась


def test_run_all_apply_mode_changes_priority_in_db(session: Session) -> None:
    publications = [_pub("https://sfr.gov.ru/n/102", "постановление ветеран боевых действий выплата")]
    spec = SourceSpec("sfr.gov.ru/press_center/news", lambda page=1: publications if page == 1 else [])

    class _DynamicLLMClient:
        def complete(self, prompt: str) -> str:
            signal = session.query(Signal).one()
            return json.dumps([{"id": signal.id, "priority": "high", "reason": "сумма выплаты"}])

    run_all(session, specs=[spec], now=NOW, llm_client=_DynamicLLMClient(), llm_priority_apply=True)

    signal = session.query(Signal).one()
    assert signal.priority == Priority.HIGH  # apply=True: LLM-сдвиг на один уровень применён
