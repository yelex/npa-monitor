"""Адаптер mos.ru — общий реестр НПА Правительства Москвы, AGENTS.md раздел 3.

Next.js-страница: список документов дублируется в статичном HTML (CSS-module классы с
build-хешем в имени, нестабильны между деплоями сайта) и в JSON
`<script id="__NEXT_DATA__">` (`props.pageProps.initialState.documentsList.data`) —
используем JSON, он устойчивее к редизайну вёрстки. Проверено вживую 2026-08-19.

Основной кандидат на whitelist-источник региональных НПА Москвы (docs/STAGE0.md,
раздел 2) — фильтра по органу-издателю нет, принадлежность конкретному ведомству
(например ДТСЗН) определяется классификатором по тексту (Фаза 4), не здесь.
"""
from __future__ import annotations

import datetime as dt
import json

from bs4 import BeautifulSoup

from parser.fetcher import fetch
from parser.models import Publication

BASE_URL = "https://www.mos.ru"
DOCUMENTS_URL = f"{BASE_URL}/authority/documents/"
SOURCE_KEY = "mos.ru/authority/documents"

MOSCOW_TZ = dt.timezone(dt.timedelta(hours=3))


def _parse_documents_page(html: str) -> list[Publication]:
    soup = BeautifulSoup(html, "html.parser")
    script = soup.select_one('script#__NEXT_DATA__[type="application/json"]')
    if script is None or not script.string:
        return []

    data = json.loads(script.string)
    try:
        items = data["props"]["pageProps"]["initialState"]["documentsList"]["data"]
    except (KeyError, TypeError):
        return []

    publications = []
    for item in items:
        doc_id = item.get("id")
        title = (item.get("title") or "").strip()
        if doc_id is None or not title:
            continue

        published_at = None
        raw_date = item.get("date_published")
        if raw_date:
            try:
                published_at = dt.datetime.strptime(raw_date, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=MOSCOW_TZ
                )
            except ValueError:
                published_at = None

        publications.append(
            Publication(
                source_key=SOURCE_KEY,
                title=title,
                url=f"{DOCUMENTS_URL}doc/{doc_id}/",
                published_at=published_at,
            )
        )
    return publications


def fetch_documents(*, page: int = 1) -> list[Publication]:
    url = DOCUMENTS_URL if page == 1 else f"{DOCUMENTS_URL}?page={page}"
    result = fetch(url, access="direct")
    return _parse_documents_page(result.text)
