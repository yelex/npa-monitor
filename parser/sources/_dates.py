"""Общий разбор ISO-дат для адаптеров источников (kremlin.py/government.py/sfr.py).

Найдено вживую: kremlin.ru отдаёт `datetime` без смещения часового пояса (в отличие
от government.ru/sfr.gov.ru, где смещение всегда есть) — `fromisoformat` в этом случае
возвращает naive datetime, а `parser/orchestrator.py` сравнивает published_at с
tz-aware `window_start` и падает `TypeError: can't compare offset-naive and
offset-aware datetimes`. Источники — российские госсайты, дата без явного пояса
всегда московская.
"""
from __future__ import annotations

import datetime as dt

MOSCOW_TZ = dt.timezone(dt.timedelta(hours=3))


def parse_iso_moscow(value: str) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MOSCOW_TZ)
    return parsed
