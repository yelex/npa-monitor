"""Тесты parser/effective_date.py, PLAN.md Фаза 4."""
from __future__ import annotations

import datetime as dt

from parser.effective_date import extract_effective_date


def test_extracts_explicit_date_after_vstupaet() -> None:
    text = "Постановление вступает в силу с 1 января 2027 г. и распространяется на..."
    assert extract_effective_date(text) == dt.date(2027, 1, 1)


def test_extracts_explicit_date_with_vstupil_past_tense() -> None:
    text = "Указ вступил в силу 19 августа 2026 года со дня подписания"
    assert extract_effective_date(text) == dt.date(2026, 8, 19)


def test_falls_back_to_publication_date_for_ot_dnya_opublikovania() -> None:
    text = "Настоящее постановление вступает в силу со дня официального опубликования"
    published_at = dt.datetime(2026, 8, 20, 6, 0, tzinfo=dt.timezone.utc)

    assert extract_effective_date(text, published_at=published_at) == dt.date(2026, 8, 20)


def test_ot_dnya_opublikovania_without_published_at_returns_none() -> None:
    text = "Настоящее постановление вступает в силу со дня официального опубликования"
    assert extract_effective_date(text, published_at=None) is None


def test_no_recognizable_pattern_returns_none() -> None:
    text = "Постановление вступает в силу по истечении 10 дней после дня опубликования"
    assert extract_effective_date(text) is None


def test_no_mention_of_entry_into_force_returns_none() -> None:
    text = "Обычная новость без упоминания вступления в силу"
    assert extract_effective_date(text) is None
