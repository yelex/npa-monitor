"""Извлечение даты вступления в силу из текста НПА, AGENTS.md раздел 4, PLAN.md Фаза 4.

Regex-эвристика первой итерацией — не обязательна для MVP (AGENTS.md раздел 4: «дата в
тексте акта может отличаться от даты публикации на портале» — уточнение, не блокер
основного пайплайна). LLM-разбор вторым проходом при низком покрытии по итогам пилота —
точка расширения, не реализована здесь (см. AGENTS.md раздел 5 «Классификация»).

Покрывает два самых частых паттерна формулировки в НПА:
- явная дата: «вступает/вступил/вступила в силу [с] DD месяц YYYY г.»;
- «со дня официального опубликования» — не даёт даты сама по себе, дата вступления в
  силу в этом случае равна дате публикации (если она известна).

Другие формулировки («по истечении 10 дней после...», «с 1 января следующего года» и
т.п.) не разбираются — возвращается `None`, вызывающий код (Фаза 6) не должен считать
это ошибкой, только «дата не извлечена».
"""
from __future__ import annotations

import datetime as dt
import re

from parser.ru_dates import RUSSIAN_MONTHS

_EXPLICIT_DATE_RE = re.compile(
    r"вступ(?:ает|ил|ила)\s+в\s+силу\s+(?:с\s+)?(\d{1,2})\s+([а-яё]+)\s+(\d{4})\s*г",
    re.IGNORECASE,
)
_FROM_PUBLICATION_RE = re.compile(
    r"вступ(?:ает|ил|ила)\s+в\s+силу\s+со?\s+дня\s+(?:его\s+)?официальн\w*\s+опубликовани\w*",
    re.IGNORECASE,
)


def extract_effective_date(text: str, *, published_at: dt.datetime | None = None) -> dt.date | None:
    match = _EXPLICIT_DATE_RE.search(text.lower())
    if match is not None:
        day, month_name, year = match.groups()
        month = RUSSIAN_MONTHS.get(month_name)
        if month is not None:
            try:
                return dt.date(int(year), month, int(day))
            except ValueError:
                pass

    if published_at is not None and _FROM_PUBLICATION_RE.search(text.lower()):
        return published_at.date()

    return None
