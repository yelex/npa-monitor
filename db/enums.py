"""Перечисления карточки сигнала (AGENTS.md, разделы 6-7)."""
from __future__ import annotations

import enum


class SignalCategory(str, enum.Enum):
    """Жизненная ситуация (ЖС), AGENTS.md раздел 1."""

    VETERANS = "veterans"  # ветераны боевых действий (ВБД)
    DISABLED = "disabled"  # люди с инвалидностью
    SVO = "svo"  # участники СВО и члены их семей


class EventType(str, enum.Enum):
    """Тип события публикации, AGENTS.md раздел 7."""

    NEW_DOCUMENT = "new_document"
    AMENDMENT = "amendment"
    REPEAL = "repeal"
    ENTRY_INTO_FORCE = "entry_into_force"
    REVIEW = "review"


class Priority(str, enum.Enum):
    """Приоритет сигнала, AGENTS.md раздел 7 (эвристика п.5.1)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SignalStatus(str, enum.Enum):
    """Статусная модель сигнала, AGENTS.md раздел 6."""

    NEW = "new"
    IN_PROGRESS = "in_progress"
    POSTPONED = "postponed"
    REJECTED = "rejected"
    SENT_TO_AGENT = "sent_to_agent"
    COMPLETED = "completed"


class RejectionReason(str, enum.Enum):
    """Причина отклонения, AGENTS.md раздел 6."""

    NOT_TARGET_CATEGORY = "not_target_category"  # Не относится к целевым категориям
    DUPLICATE = "duplicate"  # Дубликат
    NOT_NPA = "not_npa"  # Не является НПА
    OTHER = "other"  # Другое


class Region(str, enum.Enum):
    """Регион действия, AGENTS.md раздел 7 (MVP: Москва + РФ)."""

    RF = "rf"  # РФ (федеральный уровень)
    MOSCOW = "moscow"  # Москва (77) — MVP
    UNDEFINED = "undefined"  # Не определён
