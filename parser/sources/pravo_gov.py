"""Адаптер publication.pravo.gov.ru — официальное опубликование НПА, AGENTS.md раздел 3.

Листинг не в статичном HTML `/documents/daily` — список подгружается AJAX-эндпоинтом
`/Documents/search` (найден в `/js/documents.js`), отдающим HTML-фрагмент по `periodType`
(`daily`/`weekly`/`monthly`) и `index` (номер страницы). Проверено вживую 2026-08-19:
`periodType=daily` в момент проверки не дал результатов (пусто на эту дату/час),
`periodType=weekly` — 1463 документа, структура листинга подтверждена реальной вёрсткой.

RSNET: 443 фильтруется для не-РФ IP, доступ по HTTP через `RU_PROXY_URL`
(docs/STAGE0.md, раздел 2.1). Единственный источник официального опубликования
федеральных актов — обязателен для MVP несмотря на необходимость проверки на боевом VPS.
"""
from __future__ import annotations

import datetime as dt
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from parser.fetcher import fetch
from parser.models import Publication

BASE_URL = "http://publication.pravo.gov.ru"
SEARCH_URL = f"{BASE_URL}/Documents/search"
SOURCE_KEY = "publication.pravo.gov.ru"

MOSCOW_TZ = dt.timezone(dt.timedelta(hours=3))


def _extract_date(row: BeautifulSoup) -> dt.datetime | None:
    for info_div in row.select(".infoindocumentlist > div"):
        name_el = info_div.select_one(".info-name")
        data_el = info_div.select_one(".info-data")
        if name_el is None or data_el is None:
            continue
        if "Дата" not in name_el.get_text():
            continue
        try:
            return dt.datetime.strptime(data_el.get_text(strip=True), "%d.%m.%Y").replace(
                tzinfo=MOSCOW_TZ
            )
        except ValueError:
            return None
    return None


def _parse_search_page(html: str) -> list[Publication]:
    soup = BeautifulSoup(html, "html.parser")
    publications = []

    for row in soup.select(".documents-table-row"):
        link = row.select_one("a.documents-item-name")
        if link is None or not link.get("href"):
            continue

        publications.append(
            Publication(
                source_key=SOURCE_KEY,
                title=link.get_text(" ", strip=True),
                url=urljoin(BASE_URL, link["href"]),
                published_at=_extract_date(row),
            )
        )
    return publications


def fetch_documents(
    *, period: str = "daily", page: int = 1, ru_proxy_url: str | None = None
) -> list[Publication]:
    """`period` — "daily"/"weekly"/"monthly", соответствует фильтру периода на сайте."""
    url = f"{SEARCH_URL}?block=&periodType={period}&category=&index={page}"
    result = fetch(url, access="ru_proxy", ru_proxy_url=ru_proxy_url)
    return _parse_search_page(result.text)


def select_period(window_start: dt.datetime, now: dt.datetime) -> str:
    """Адаптивный `periodType` по размеру окна пропуска (docs/SPEC_pravo_gov_pagination_depth.md,
    п.2): при обычном ежедневном прогоне (окно <=1 дня) листинг ленты небольшой и `daily`
    его покрывает; после сбоя/простоя (окно 1-7 дней) объём вырастает на порядок
    (~2500 документов/неделя) — нужен `weekly`; при более длинном пропуске (>7 дней) —
    `monthly`, чтобы не упереться в предохранитель страниц раньше, чем в начало окна.
    Чистая функция даты, без сетевого вызова — фолбэк на пустой `daily`-ответ отдельно,
    см. `parser.orchestrator.SourceSpec.period_fallback`.

    docs/SPEC_pravo_gov_pagination_depth.md, ревью п.1: `periodType=daily` фильтрует по
    «Дата опубликования» строго в рамках текущего календарного дня источника (МСК), не
    «последние 24 часа от now». Если окно `[window_start, now)` пересекает полночь МСК
    (`window_start` и `now` — разные календарные даты), часть окна лежит на вчерашнем
    дне, которого в `daily`-листинге просто нет ни на какой странице — это не глубже по
    сортировке, а физическое отсутствие данных, фолбэк по пустой первой странице (п.3)
    его не поймает, если сегодня уже есть свои публикации (страница непустая). Поэтому
    `daily` выбирается только когда окно целиком внутри одного календарного дня МСК —
    иначе сразу `weekly` (гарантированно покрывает и вчера, и сегодня)."""
    span = now - window_start
    if span > dt.timedelta(days=7):
        return "monthly"
    if span > dt.timedelta(days=1):
        return "weekly"
    if window_start.astimezone(MOSCOW_TZ).date() != now.astimezone(MOSCOW_TZ).date():
        return "weekly"
    return "daily"
