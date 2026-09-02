"""Адаптер publication.pravo.gov.ru — официальное опубликование НПА, AGENTS.md раздел 3.

Листинг не в статичном HTML `/documents/daily` — список подгружается AJAX-эндпоинтом
`/Documents/search` (найден в `/js/documents.js`), отдающим HTML-фрагмент по `periodType`
и `index` (номер страницы).

docs/SPEC_pravo_gov_day_by_day.md (диагностика 02.09, /tmp/claude_pravo_diag.md):
`periodType=daily/weekly/monthly` — НЕ окно "последние N дней от now", а фиксированный
ТЕКУЩИЙ календарный период (сегодня / неделя Пн-Вс / текущий месяц), не сдвигается назад
ни при какой пагинации `index`; при простое дольше текущей недели/месяца документы
пропущенного периода физически отсутствуют в выдаче на любой странице. Единственный
подтверждённый вживую способ достать произвольный день прошлого — `periodType=day&
date=DD.MM.YYYY` (параметр `date` игнорируется для `weekly`/`monthly`, работает только с
`day`). Поэтому обход ведётся по календарным дням МСК (`build_day_plan`), не эскалацией
period — см. `parser.orchestrator._process_source_by_day`.

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
    *,
    period: str = "day",
    date: str | None = None,
    page: int = 1,
    ru_proxy_url: str | None = None,
) -> list[Publication]:
    """`period` — фильтр периода на сайте ("day" — обход по календарным дням,
    docs/SPEC_pravo_gov_day_by_day.md; "weekly"/"monthly" оставлены как параметр URL для
    обратной совместимости, но оркестратором больше не используются — см. модульный
    докстринг). `date` — `DD.MM.YYYY`, обязателен для `period="day"`, определяет
    конкретный календарный день (МСК); без него сайт отдаёт текущий период."""
    url = f"{SEARCH_URL}?block=&periodType={period}&category=&index={page}"
    if date is not None:
        url += f"&date={date}"
    result = fetch(url, access="ru_proxy", ru_proxy_url=ru_proxy_url)
    return _parse_search_page(result.text)


def build_day_plan(window_start: dt.datetime, now: dt.datetime) -> list[dt.date]:
    """docs/SPEC_pravo_gov_day_by_day.md: план обхода — список календарных дней МСК от
    `window_start` до `now` включительно, по возрастанию. Заменяет эскалацию
    `periodType daily->weekly->monthly` (docs/SPEC_pravo_gov_pagination_depth.md) —
    диагностика 02.09 показала, что `weekly`/`monthly` не окно "последние N дней", а
    фиксированный текущий период, не достижимый для catch-up старше текущей недели/
    месяца. Обычный ежедневный прогон — план из 1-2 дней; простой в месяц — ~30-31 дня."""
    start_date = window_start.astimezone(MOSCOW_TZ).date()
    end_date = now.astimezone(MOSCOW_TZ).date()
    days = []
    current = start_date
    while current <= end_date:
        days.append(current)
        current += dt.timedelta(days=1)
    return days
