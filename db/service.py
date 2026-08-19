"""Общий слой поверх моделей: переходы статусов, создание сигнала, дедуп.

Переходы статусов — по статусной модели AGENTS.md раздел 6 и PLAN.md п.5.5
(повторное открытие «Отклонён» через /reopen).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.enums import EventType, Priority, Region, RejectionReason, SignalCategory, SignalStatus
from db.models import DocumentSeen, Signal, SignalCategoryLink, SourceState, StatusHistory

# Допустимые переходы статуса сигнала (AGENTS.md раздел 6):
# - Новый -> В работе / Отклонён (эксперт: "Взять в работу" / "Отклонить")
# - В работе -> Отложен / Отклонён / Передан агенту (эксперт принимает решение)
# - Отложен -> В работе (сигнал возвращается в очередь, эксперт снова берёт в работу)
# - Передан агенту -> Завершён (после проверки результата агента)
# - Отклонён -> Новый (повторное открытие, PLAN.md п.5.5, /reopen)
# - Завершён — конечный статус, переходов из него нет.
ALLOWED_TRANSITIONS: dict[SignalStatus, frozenset[SignalStatus]] = {
    SignalStatus.NEW: frozenset({SignalStatus.IN_PROGRESS, SignalStatus.REJECTED}),
    SignalStatus.IN_PROGRESS: frozenset(
        {SignalStatus.POSTPONED, SignalStatus.REJECTED, SignalStatus.SENT_TO_AGENT}
    ),
    SignalStatus.POSTPONED: frozenset({SignalStatus.IN_PROGRESS}),
    SignalStatus.SENT_TO_AGENT: frozenset({SignalStatus.COMPLETED}),
    SignalStatus.REJECTED: frozenset({SignalStatus.NEW}),
    SignalStatus.COMPLETED: frozenset(),
}


class InvalidStatusTransition(ValueError):
    def __init__(self, from_status: SignalStatus, to_status: SignalStatus) -> None:
        super().__init__(f"Недопустимый переход статуса: {from_status.value} -> {to_status.value}")
        self.from_status = from_status
        self.to_status = to_status


def create_signal(
    session: Session,
    *,
    event_type: EventType,
    priority: Priority,
    source_url: str,
    categories: list[SignalCategory],
    region: Region = Region.UNDEFINED,
    title: str | None = None,
    requisites: str | None = None,
    measure_id: str | None = None,
) -> Signal:
    """Создаёт сигнал в статусе «Новый» (парсер, AGENTS.md раздел 6)."""
    signal = Signal(
        event_type=event_type,
        priority=priority,
        source_url=source_url,
        region=region,
        title=title,
        requisites=requisites,
        measure_id=measure_id,
        status=SignalStatus.NEW,
    )
    signal.categories = [SignalCategoryLink(category=category) for category in categories]
    session.add(signal)
    session.flush()

    session.add(
        StatusHistory(signal_id=signal.id, from_status=None, to_status=SignalStatus.NEW)
    )
    return signal


def transition_status(
    session: Session,
    signal: Signal,
    new_status: SignalStatus,
    *,
    changed_by: str | None = None,
    reason: str | None = None,
    rejection_reason: RejectionReason | None = None,
    rejection_comment: str | None = None,
) -> Signal:
    """Переводит сигнал в новый статус, проверяя допустимость перехода и записывая аудит."""
    current_status = signal.status
    allowed = ALLOWED_TRANSITIONS.get(current_status, frozenset())
    if new_status not in allowed:
        raise InvalidStatusTransition(current_status, new_status)

    if new_status == SignalStatus.REJECTED and rejection_reason is None:
        raise ValueError("Отклонение сигнала требует причины (rejection_reason)")

    signal.status = new_status
    if new_status == SignalStatus.REJECTED:
        signal.rejection_reason = rejection_reason
        signal.rejection_comment = rejection_comment
    elif new_status == SignalStatus.NEW:
        # Повторное открытие — сбрасываем причину предыдущего отклонения.
        signal.rejection_reason = None
        signal.rejection_comment = None

    session.add(
        StatusHistory(
            signal_id=signal.id,
            from_status=current_status,
            to_status=new_status,
            changed_by=changed_by,
            reason=reason,
        )
    )
    session.flush()
    return signal


def register_document_seen(
    session: Session,
    *,
    source_key: str,
    doc_url: str,
    signal_id: int | None = None,
) -> tuple[DocumentSeen, bool]:
    """Дедуп публикаций по URL (AGENTS.md раздел 4/11).

    Возвращает (запись, created) — created=False, если публикация уже была обработана
    ранее и новую карточку сигнала создавать не нужно.
    """
    existing = session.scalar(select(DocumentSeen).where(DocumentSeen.doc_url == doc_url))
    if existing is not None:
        return existing, False

    document = DocumentSeen(source_key=source_key, doc_url=doc_url, signal_id=signal_id)
    session.add(document)
    session.flush()
    return document, True


def update_source_state(
    session: Session,
    *,
    source_key: str,
    success_at: dt.datetime,
    last_seen_publication_date: dt.date | None = None,
) -> SourceState:
    """Upsert даты последнего успешного обхода источника (доверстывание, AGENTS.md раздел 5)."""
    state = session.get(SourceState, source_key)
    if state is None:
        state = SourceState(source_key=source_key)
        session.add(state)

    state.last_success_at = success_at
    if last_seen_publication_date is not None:
        state.last_seen_publication_date = last_seen_publication_date
    session.flush()
    return state
