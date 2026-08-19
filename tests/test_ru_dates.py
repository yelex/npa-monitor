"""Тесты parser/ru_dates.py."""
from __future__ import annotations

import datetime as dt

from parser.ru_dates import parse_russian_date


def test_parse_russian_date_basic() -> None:
    assert parse_russian_date("24 июля 2026") == dt.datetime(2026, 7, 24, tzinfo=dt.timezone.utc)


def test_parse_russian_date_with_trailing_word_goda() -> None:
    assert parse_russian_date("14 Августа 2026 года") == dt.datetime(2026, 8, 14, tzinfo=dt.timezone.utc)


def test_parse_russian_date_embedded_in_sentence() -> None:
    text = "Документ вступает в силу с 1 января 2027 года после опубликования"
    assert parse_russian_date(text) == dt.datetime(2027, 1, 1, tzinfo=dt.timezone.utc)


def test_parse_russian_date_unknown_month_returns_none() -> None:
    assert parse_russian_date("24 непонятногомесяца 2026") is None


def test_parse_russian_date_no_date_returns_none() -> None:
    assert parse_russian_date("здесь нет даты вообще") is None


def test_parse_russian_date_invalid_day_returns_none() -> None:
    assert parse_russian_date("32 июля 2026") is None
