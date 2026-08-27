"""Схема БД сигналов, AGENTS.md раздел 7, PLAN.md раздел 2.

SQLite WAL, без Alembic в MVP — таблицы создаются через ``Base.metadata.create_all``
(см. ``db/session.py``). Миграции — на следующем этапе.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    JSON,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from db.enums import (
    REGION_UNDEFINED,
    EventType,
    Priority,
    RejectionReason,
    SignalCategory,
    SignalStatus,
    SignalType,
)
from db.types import UTCDateTime


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class Signal(Base):
    """Карточка сигнала (AGENTS.md раздел 7)."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)

    event_type: Mapped[EventType] = mapped_column(Enum(EventType, native_enum=False, length=32))
    priority: Mapped[Priority] = mapped_column(Enum(Priority, native_enum=False, length=16))
    status: Mapped[SignalStatus] = mapped_column(
        Enum(SignalStatus, native_enum=False, length=32),
        default=SignalStatus.NEW,
        nullable=False,
    )

    title: Mapped[str | None] = mapped_column(Text, default=None)  # Название документа
    requisites: Mapped[str | None] = mapped_column(Text, default=None)  # Реквизиты
    # Фаза 13 (docs/SPEC_region_expansion.md): код региона — строка, значение
    # `RegionEntry.code` из `data/regions.yaml` (89 регионов + rf/moscow/undefined),
    # не Python-enum — справочник растёт без миграции схемы.
    region: Mapped[str] = mapped_column(String(64), default=REGION_UNDEFINED)
    source_url: Mapped[str] = mapped_column(Text)  # Ссылка на публикацию

    # Ссылка на НПА, подтверждённая экспертом (раздел 10, шаг 5) — отдельно от source_url,
    # т.к. публикация и первоисточник акта могут не совпадать.
    npa_link: Mapped[str | None] = mapped_column(Text, default=None)

    # Привязка к карточке меры внешнего контура «агента автообновления» (раздел 3).
    # Не FK: measureId живёт в другой системе, здесь только строковый идентификатор.
    measure_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)

    # docs/SPEC_signal_type_measure_select.md: тип сигнала (change/new, шаг FSM после
    # региона) и row_hash записи базы мер на момент выбора (для сравнения версий
    # коннектором) — null для new / «нет в базе». Колонки добавлены поверх уже
    # развёрнутой схемы через db/session.py::_ensure_columns (Alembic не используется).
    signal_type: Mapped[SignalType | None] = mapped_column(
        Enum(SignalType, native_enum=False, length=16), default=None
    )
    measure_row_hash: Mapped[str | None] = mapped_column(String(64), default=None)

    rejection_reason: Mapped[RejectionReason | None] = mapped_column(
        Enum(RejectionReason, native_enum=False, length=32), default=None
    )
    rejection_comment: Mapped[str | None] = mapped_column(Text, default=None)

    created_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=_utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow)

    categories: Mapped[list[SignalCategoryLink]] = relationship(
        back_populates="signal", cascade="all, delete-orphan"
    )
    history: Mapped[list[StatusHistory]] = relationship(
        back_populates="signal", cascade="all, delete-orphan", order_by="StatusHistory.changed_at"
    )

    __table_args__ = (
        Index("ix_signals_status_created_at", "status", "created_at"),
    )


class SignalCategoryLink(Base):
    """M2M сигнал -> ЖС (AGENTS.md раздел 1)."""

    __tablename__ = "signal_categories"

    signal_id: Mapped[int] = mapped_column(
        ForeignKey("signals.id", ondelete="CASCADE"), primary_key=True
    )
    category: Mapped[SignalCategory] = mapped_column(
        Enum(SignalCategory, native_enum=False, length=16), primary_key=True
    )

    signal: Mapped[Signal] = relationship(back_populates="categories")


class StatusHistory(Base):
    """Аудит переходов статуса (нужен для напоминаний >3 дней, AGENTS.md раздел 11)."""

    __tablename__ = "status_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id", ondelete="CASCADE"))
    from_status: Mapped[SignalStatus | None] = mapped_column(
        Enum(SignalStatus, native_enum=False, length=32), default=None
    )
    to_status: Mapped[SignalStatus] = mapped_column(Enum(SignalStatus, native_enum=False, length=32))
    changed_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=_utcnow)
    changed_by: Mapped[str | None] = mapped_column(String(128), default=None)  # telegram id/имя эксперта
    reason: Mapped[str | None] = mapped_column(Text, default=None)

    signal: Mapped[Signal] = relationship(back_populates="history")

    __table_args__ = (Index("ix_status_history_signal_id", "signal_id"),)


class SourceState(Base):
    """Per-источник дата последнего успешного обхода (AGENTS.md раздел 5, доверстывание)."""

    __tablename__ = "sources_state"

    source_key: Mapped[str] = mapped_column(String(128), primary_key=True)  # напр. "mintrud.gov.ru/docs"
    last_success_at: Mapped[dt.datetime | None] = mapped_column(UTCDateTime, default=None)
    last_seen_publication_date: Mapped[dt.date | None] = mapped_column(default=None)
    updated_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=_utcnow, onupdate=_utcnow)


class DocumentSeen(Base):
    """Дедуп публикаций независимо от создания сигнала (AGENTS.md раздел 4, 11)."""

    __tablename__ = "documents_seen"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(128))
    doc_url: Mapped[str] = mapped_column(Text)
    # Заголовок публикации на момент обнаружения — материал для вторичного дедупа по
    # содержанию (PLAN.md Фаза 9 п.2, docs/SPEC_content_dedup.md), не показывается
    # эксперту. NULL у записей, созданных до этой задачи.
    title: Mapped[str | None] = mapped_column(Text, default=None)
    first_seen_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=_utcnow)
    signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL"), default=None
    )

    __table_args__ = (UniqueConstraint("doc_url", name="uq_documents_seen_doc_url"),)


class Expert(Base):
    """Whitelist экспертов бота (AGENTS.md раздел 9, 11)."""

    __tablename__ = "experts"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(128), default=None)
    is_active: Mapped[bool] = mapped_column(default=True)
    added_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=_utcnow)


class SignalResult(Base):
    """Результат агента автообновления, сохранённый до архивации spool-файла
    (docs/SPEC_result_edit.md §3.4, ревью №3) — источник `changes[]`/`selection`
    для write-back в `measure_overrides`, переживает рестарт бота и архивацию
    `results/<task_id>.json` в `results/.processed/`."""

    __tablename__ = "signal_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL"), default=None
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    # Промежуточный выбор аналитика по чекбоксам карточки (accept/skip/custom/delete
    # по индексу поля в payload["changes"]) — JSON, не FSM: переживает рестарт бота
    # (спека §3.4).
    selection: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    received_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=_utcnow)


class MeasureOverride(Base):
    """Overlay ручной правки поля меры поверх KB-снапшота (docs/SPEC_result_edit.md
    §3.3) — KB-файл сам не патчится (перезаливаемый снапшот), правки применяются
    поверх него при экспорте (`scripts/export_kb.py`) и при чтении «эффективного»
    значения (`db/overrides.py::effective_value`).

    Один override = одна правка одного поля; повторная правка того же поля —
    новая строка (история), актуальность — по последнему `changed_at`.
    `base_row_hash` — row_hash KB-записи на момент, когда считался дифф
    (`Signal.measure_row_hash`), для детекта STALE на стороне бота.
    """

    __tablename__ = "measure_overrides"

    id: Mapped[int] = mapped_column(primary_key=True)
    measure_id: Mapped[str] = mapped_column(String(64), index=True)
    field: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[Any] = mapped_column(JSON, nullable=True, default=None)
    new_value: Mapped[Any] = mapped_column(JSON, nullable=True, default=None)  # null = «удалено аналитиком»
    signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("signals.id", ondelete="SET NULL"), default=None
    )
    source: Mapped[str] = mapped_column(String(16), default="agent_diff")  # agent_diff | manual
    changed_by: Mapped[str | None] = mapped_column(String(128), default=None)
    changed_at: Mapped[dt.datetime] = mapped_column(UTCDateTime, default=_utcnow)
    task_id: Mapped[str] = mapped_column(String(64))
    base_row_hash: Mapped[str | None] = mapped_column(String(64), default=None)

    __table_args__ = (
        # Дедуп ревью №4: тот же результат агента не применяется к одному полю
        # меры дважды (даблтап/ретрай «Применить» игнорируется).
        UniqueConstraint("measure_id", "field", "task_id", name="uq_measure_overrides_dedup"),
    )
