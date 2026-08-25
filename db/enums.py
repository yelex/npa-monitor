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


#: Регион действия, AGENTS.md раздел 7. `Signal.region`/`RegionEntry.code` — строка,
#: источник истины `data/regions.yaml` (Фаза 13, docs/SPEC_region_expansion.md) — не
#: Python-enum, справочник теперь покрывает все 89 регионов и растёт без правки кода.
#: Эти три сентинела остаются константами, т.к. участвуют в коде отдельными ветками
#: (федеральный уровень / не определён).
REGION_RF = "rf"
REGION_MOSCOW = "moscow"
REGION_UNDEFINED = "undefined"


class SignalType(str, enum.Enum):
    """Тип сигнала, docs/SPEC_signal_type_measure_select.md: контракт задачи v2 для
    коннектора npa-somas — изменение существующей меры (с выбором measure_id из базы)
    или новая мера (measure_id остаётся null)."""

    CHANGE = "change"  # изменение существующей меры
    NEW = "new"  # новая мера
