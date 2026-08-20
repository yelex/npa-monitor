"""Тесты parser/sources/_dates.py.

Регрессия (2026-08-20): kremlin.ru отдаёт datetime без смещения пояса, что приводило
к TypeError при сравнении с tz-aware окном поиска в orchestrator.py.
"""
from __future__ import annotations

import datetime as dt

from parser.sources._dates import MOSCOW_TZ, parse_iso_moscow


def test_parse_iso_moscow_assigns_moscow_tz_when_naive() -> None:
    assert parse_iso_moscow("2026-08-12") == dt.datetime(2026, 8, 12, tzinfo=MOSCOW_TZ)


def test_parse_iso_moscow_keeps_explicit_offset() -> None:
    assert parse_iso_moscow("2026-08-19T10:00:00+04:00") == dt.datetime(
        2026, 8, 19, 10, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=4))
    )


def test_parse_iso_moscow_returns_none_on_invalid_input() -> None:
    assert parse_iso_moscow("не дата") is None
