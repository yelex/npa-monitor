"""Общие типы парсера, PLAN.md Фаза 3."""
from __future__ import annotations

import dataclasses
import datetime as dt


@dataclasses.dataclass(frozen=True)
class Publication:
    """Одна запись из листинга источника — вход для классификатора (Фаза 4)."""

    source_key: str  # ключ источника, совпадает с db.models.SourceState.source_key
    title: str
    url: str  # абсолютный URL
    published_at: dt.datetime | None  # None, если источник не даёт машиночитаемую дату
    summary: str | None = None
