"""Адаптер kremlin.ru — указы и поручения Президента РФ, AGENTS.md раздел 3.

Листинг `/acts/news` — server-rendered, schema.org-микроразметка (`itemtype=NewsArticle`),
подтверждено вживую 2026-08-19 реальной вёрсткой. RSNET: доступ по HTTP через
`RU_PROXY_URL` (docs/STAGE0.md, раздел 2.1).
"""
from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from parser.fetcher import fetch
from parser.models import Publication
from parser.sources._dates import parse_iso_moscow

BASE_URL = "http://www.kremlin.ru"
NEWS_URL = f"{BASE_URL}/acts/news"
SOURCE_KEY = "kremlin.ru/acts/news"


def _parse_news_page(html: str) -> list[Publication]:
    soup = BeautifulSoup(html, "html.parser")
    publications = []

    for entry in soup.select('div[itemtype="http://schema.org/NewsArticle"]'):
        link = entry.select_one('a[itemprop="url"]')
        title_el = entry.select_one('span[itemprop="name"]')
        if link is None or title_el is None or not link.get("href"):
            continue

        published_at = None
        time_el = entry.select_one('time[itemprop="datePublished"]')
        if time_el is not None and time_el.get("datetime"):
            published_at = parse_iso_moscow(time_el["datetime"])

        publications.append(
            Publication(
                source_key=SOURCE_KEY,
                title=title_el.get_text(strip=True),
                url=urljoin(BASE_URL, link["href"]),
                published_at=published_at,
            )
        )
    return publications


def fetch_news(*, page: int = 1, ru_proxy_url: str | None = None) -> list[Publication]:
    url = NEWS_URL if page == 1 else f"{NEWS_URL}/page/{page}"
    result = fetch(url, access="ru_proxy", ru_proxy_url=ru_proxy_url)
    return _parse_news_page(result.text)
