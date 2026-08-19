"""Разбор дат в русском текстовом формате («24 июля 2026 года»), PLAN.md Фаза 3/4.

Общий модуль — было продублировано в `parser/sources/mintrud.py` и
`parser/sources/msupport_dszn.py`, вынесено сюда при разработке `parser/effective_date.py`
(Фаза 4), чтобы не плодить третью копию словаря месяцев.
"""
from __future__ import annotations

import datetime as dt
import re

RUSSIAN_MONTHS: dict[str, int] = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}

_DATE_RE = re.compile(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", re.IGNORECASE)


def parse_russian_date(text: str, *, tzinfo: dt.tzinfo = dt.timezone.utc) -> dt.datetime | None:
    """Ищет в тексте дату вида «DD <месяц родительный падеж> YYYY» (год необязательно
    с точкой/словом «года» после — регэксп ищет только сам паттерн даты)."""
    match = _DATE_RE.search(text.lower())
    if match is None:
        return None
    day, month_name, year = match.groups()
    month = RUSSIAN_MONTHS.get(month_name)
    if month is None:
        return None
    try:
        return dt.datetime(int(year), month, int(day), tzinfo=tzinfo)
    except ValueError:
        return None
