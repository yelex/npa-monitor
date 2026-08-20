"""Адаптер government.ru — постановления и распоряжения Правительства РФ, AGENTS.md
раздел 3.

Листинг `/docs/` — server-rendered, подтверждено вживую 2026-08-19 реальной вёрсткой.
RSNET: HTTPS у этого домена не отвечает (таймаут) даже плейн-запросом, доступ по HTTP
через `RU_PROXY_URL` (docs/STAGE0.md, раздел 2.1).
"""
from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from parser.fetcher import fetch
from parser.models import Publication
from parser.sources._dates import parse_iso_moscow

BASE_URL = "http://government.ru"
DOCS_URL = f"{BASE_URL}/docs/"
SOURCE_KEY = "government.ru/docs"


def _parse_docs_page(html: str) -> list[Publication]:
    soup = BeautifulSoup(html, "html.parser")
    publications = []

    for headline in soup.select("div.headline"):
        link = headline.select_one("a.headline__link")
        title_el = headline.select_one("span.headline_title_link")
        if link is None or title_el is None or not link.get("href"):
            continue

        published_at = None
        time_el = headline.select_one("span.headline_date time")
        if time_el is not None and time_el.get("datetime"):
            published_at = parse_iso_moscow(time_el["datetime"])

        lead_el = headline.select_one("span.headline_lead")

        publications.append(
            Publication(
                source_key=SOURCE_KEY,
                title=title_el.get_text(strip=True),
                url=urljoin(BASE_URL, link["href"]),
                published_at=published_at,
                summary=lead_el.get_text(strip=True) if lead_el is not None else None,
            )
        )
    return publications


def fetch_docs(*, page: int = 1, ru_proxy_url: str | None = None) -> list[Publication]:
    url = DOCS_URL if page == 1 else f"{DOCS_URL}?page={page}"
    result = fetch(url, access="ru_proxy", ru_proxy_url=ru_proxy_url)
    return _parse_docs_page(result.text)
