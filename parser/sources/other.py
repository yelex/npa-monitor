"""Адаптер «иных» источников (СМИ/агрегаторы) — AGENTS.md раздел 3-4.

Это контекстные источники: публикация отсюда сама по себе не основание для сигнала без
подтверждения федеральным/региональным источником (см. AGENTS.md раздел 4). Все пять —
RSS-фиды (проверено вживую 2026-08-20), URL — в `data/sources.yaml`, группа `other`.
`tass.ru` отдаёт пустой ответ на дефолтный UA библиотек — как и остальной парсер,
использует браузероподобный UA из `parser/fetcher.py`.

`docs.cntd.ru` (упомянут в AGENTS.md как `/comments`) сетевого доступа не даёт вовсе —
не реализован, см. `data/sources.yaml` (`access: unsupported`) и `docs/STAGE0.md`.
"""
from __future__ import annotations

import datetime as dt

import feedparser

from parser.fetcher import SourceUnavailable, fetch
from parser.models import Publication

FEEDS: dict[str, str] = {
    "garant.ru": "https://rss.garant.ru/news/",
    "consultant.ru": "https://www.consultant.ru/rss/hotdocs.xml",
    "tass.ru": "https://tass.ru/rss/v2.xml",
    "ria.ru": "https://ria.ru/export/rss2/index.xml",
    "rg.ru": "https://rg.ru/xml/index.xml",
}


def _parse_feed(source_key: str, raw_xml: str) -> list[Publication]:
    parsed = feedparser.parse(raw_xml)
    publications = []

    for entry in parsed.entries:
        link = entry.get("link")
        title = entry.get("title")
        if not link or not title:
            continue

        published_at = None
        published_struct = entry.get("published_parsed")
        if published_struct is not None:
            published_at = dt.datetime(*published_struct[:6], tzinfo=dt.timezone.utc)

        publications.append(
            Publication(
                source_key=source_key,
                title=title,
                url=link,
                published_at=published_at,
                summary=entry.get("summary") or None,
            )
        )
    return publications


def fetch_feed(domain: str) -> list[Publication]:
    """`domain` — один из ключей `FEEDS` (совпадает с `data/sources.yaml`, группа `other`)."""
    if domain not in FEEDS:
        raise ValueError(f"нет RSS-фида для домена {domain!r}; известные: {sorted(FEEDS)}")
    result = fetch(FEEDS[domain], access="direct")
    return _parse_feed(domain, result.text)


def fetch_all() -> dict[str, list[Publication]]:
    """Обходит все известные RSS-фиды; недоступность одного не прерывает остальные
    (AGENTS.md раздел 12 «Источник недоступен» — пропуск до следующего цикла)."""
    results: dict[str, list[Publication]] = {}
    for domain in FEEDS:
        try:
            results[domain] = fetch_feed(domain)
        except SourceUnavailable:
            results[domain] = []
    return results
