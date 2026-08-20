"""Адаптер sfr.gov.ru — Социальный фонд России, AGENTS.md раздел 3, PLAN.md Фаза 3.

Листинг новостей (`press_center/news/`) — плоский, подтверждён живым запросом при
разработке этого адаптера: каждая запись — `<article class="re-news__article">` с
ISO-датой в `<time datetime=...>`, пагинация `?page=N`.

Раздел нормативных актов фонда (по данным `docs/STAGE0.md` — `/about/prf_order/`,
редиректит на `/order/`) на практике оказался категорийным индексом, а не плоским
листингом: там просто ссылки на подстраницы вроде `/order/voennoslujaschim` («Меры
социальной поддержки военнослужащим и членам их семей») и `/order/families`. Сканирование
этих подстраниц как отдельных листингов — не входит в эту итерацию, см. PLAN.md
(зафиксировано как уточнение к `docs/STAGE0.md`).
"""
from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from parser.fetcher import fetch
from parser.models import Publication
from parser.sources._dates import parse_iso_moscow

BASE_URL = "https://sfr.gov.ru"
NEWS_URL = f"{BASE_URL}/press_center/news/"
SOURCE_KEY = "sfr.gov.ru/press_center/news"


def _parse_news_page(html: str) -> list[Publication]:
    soup = BeautifulSoup(html, "html.parser")
    publications = []

    for article in soup.select("article.re-news__article"):
        link = article.select_one("a.re-news__article-link")
        title_el = article.select_one("h3.re-news__article-title")
        if link is None or title_el is None or not link.get("href"):
            continue

        published_at = None
        time_el = article.select_one("time.re-news__article-time")
        if time_el is not None and time_el.get("datetime"):
            published_at = parse_iso_moscow(time_el["datetime"])

        summary_el = article.select_one("p.re-news__article-description")

        publications.append(
            Publication(
                source_key=SOURCE_KEY,
                title=title_el.get_text(strip=True),
                url=urljoin(BASE_URL, link["href"]),
                published_at=published_at,
                summary=summary_el.get_text(strip=True) if summary_el else None,
            )
        )
    return publications


def fetch_news(*, page: int = 1) -> list[Publication]:
    url = NEWS_URL if page == 1 else f"{NEWS_URL}?page={page}"
    result = fetch(url, access="direct")
    return _parse_news_page(result.text)
