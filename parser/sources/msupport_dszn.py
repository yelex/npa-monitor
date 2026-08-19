"""Адаптер msupport.dszn.ru — Единый центр поддержки участников СВО (Москва),
AGENTS.md раздел 3.

Разъяснительный портал ведомства (структура ДТСЗН Москвы), не листинг НПА —
вспомогательный источник для СВО-сигналов, не единственное основание для сигнала
(AGENTS.md раздел 4). Ссылки в карточках ведут на другой домен, `dszn.ru`
(`press-center/news/...`) — это ожидаемо, оба домена в белом списке (data/regions.yaml).

Листинг `/news` (не главная страница — там всего 4 карточки-тизера) — подтверждено
вживую 2026-08-19: 12 карточек на странице, пагинация `/news/page-N`.
"""
from __future__ import annotations

from bs4 import BeautifulSoup

from parser.fetcher import fetch
from parser.models import Publication
from parser.ru_dates import parse_russian_date

BASE_URL = "https://msupport.dszn.ru"
NEWS_URL = f"{BASE_URL}/news"
SOURCE_KEY = "msupport.dszn.ru/news"


def _parse_news_page(html: str) -> list[Publication]:
    soup = BeautifulSoup(html, "html.parser")
    publications = []

    for card in soup.select('a[href*="dszn.ru/press-center/news/"]'):
        title_el = card.select_one("p b")
        if title_el is None or not card.get("href"):
            continue

        date_el = card.select_one("span.date")
        published_at = parse_russian_date(date_el.get_text()) if date_el is not None else None

        publications.append(
            Publication(
                source_key=SOURCE_KEY,
                title=title_el.get_text(strip=True),
                url=card["href"],  # уже абсолютный, ведёт на dszn.ru
                published_at=published_at,
            )
        )
    return publications


def fetch_news(*, page: int = 1) -> list[Publication]:
    url = NEWS_URL if page == 1 else f"{NEWS_URL}/page-{page}"
    result = fetch(url, access="direct")
    return _parse_news_page(result.text)
