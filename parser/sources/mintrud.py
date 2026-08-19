"""Адаптер mintrud.gov.ru — Минтруд России, AGENTS.md раздел 3, PLAN.md Фаза 3.

Требует браузероподобный User-Agent (см. `parser/fetcher.py::DEFAULT_HEADERS`) — сайт
отдаёт 403 на идентифицирующий UA бота, это WAF-эвристика, не запрет в `robots.txt`
(путь `/docs` не в `Disallow` для `User-agent: *`, проверено вживую).

Раздел `/docs` — нормативные акты министерства, листинг: `<div class="post-list">` без
машиночитаемой даты (только текст вида «24 июля 2026», русское название месяца).
Пагинация — `?directoryId=128&page=N`.
"""
from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from parser.fetcher import fetch
from parser.models import Publication
from parser.ru_dates import parse_russian_date

BASE_URL = "https://mintrud.gov.ru"
DOCS_URL = f"{BASE_URL}/docs"
SOURCE_KEY = "mintrud.gov.ru/docs"
DEFAULT_DIRECTORY_ID = 128


def _parse_docs_page(html: str) -> list[Publication]:
    soup = BeautifulSoup(html, "html.parser")
    publications = []

    for item in soup.select("div.post-list"):
        link = item.select_one("a.text-black")
        title_el = item.select_one("p.post-name")
        if link is None or title_el is None or not link.get("href"):
            continue

        date_el = item.select_one("p.page-date")
        published_at = parse_russian_date(date_el.get_text()) if date_el is not None else None

        publications.append(
            Publication(
                source_key=SOURCE_KEY,
                title=title_el.get_text(strip=True),
                url=urljoin(BASE_URL, link["href"]),
                published_at=published_at,
            )
        )
    return publications


def fetch_docs(*, page: int = 1, directory_id: int = DEFAULT_DIRECTORY_ID) -> list[Publication]:
    url = DOCS_URL if page == 1 else f"{DOCS_URL}?directoryId={directory_id}&page={page}"
    result = fetch(url, access="direct")
    return _parse_docs_page(result.text)
