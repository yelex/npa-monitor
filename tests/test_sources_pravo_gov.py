"""Тесты parser/sources/pravo_gov.py на реальной вёрстке /Documents/search
(фрагмент снят вживую 2026-08-19, periodType=weekly)."""
from __future__ import annotations

import datetime as dt

import httpx
import pytest

from parser.sources.pravo_gov import MOSCOW_TZ, SEARCH_URL, SOURCE_KEY, build_day_plan, fetch_documents

REAL_SEARCH_FRAGMENT = """
<div class="documents-container"><div class="documents-table">
<div class="documents-table-row">
  <div class="documents-table-cell"><div class="documents-item-number">1</div></div>
  <div class="documents-table-cell documents-fill">
    <div class="row documents-items">
      <div class="col-xl-9 col-md-12">
        <a href="/document/0001202608190017">
          <img class="documents-document-image" alt="Документ" src="/images/document.svg" />
        </a>
        <a class="documents-item-name" href="/document/0001202608190017">
          Распоряжение Правительства Российской Федерации от 19.08.2026 № 2198-р
          <br/>
          "О Торговом представителе Российской Федерации в Турецкой Республике"
        </a>
      </div>
      <div class="col-xl-3 col-md-12">
        <div class="infoindocumentlist">
          <div><span class="info-name">Номер опубликования:</span><span class="info-data">0001202608190017</span></div>
          <div><span class="info-name">Дата опубликования:</span><span class="info-data">19.08.2026</span></div>
        </div>
      </div>
    </div>
  </div>
</div>
</div></div>
"""


def test_fetch_documents_parses_real_markup_fragment(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client_cls = httpx.Client
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(
            200, content=REAL_SEARCH_FRAGMENT.encode(), headers={"content-type": "text/html; charset=utf-8"}
        )

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client_cls(transport=httpx.MockTransport(handler)))

    # docs/SPEC_pravo_gov_day_by_day.md: обход теперь по календарным дням, не periodType
    # weekly/monthly (подтверждено диагностикой 02.09 нерабочим для catch-up) — та же
    # живая вёрстка, но через periodType=day&date=.
    publications = fetch_documents(period="day", date="19.08.2026", ru_proxy_url="http://proxy.local:8888")

    assert seen_urls == [f"{SEARCH_URL}?block=&periodType=day&category=&index=1&date=19.08.2026"]
    assert len(publications) == 1
    pub = publications[0]
    assert pub.source_key == SOURCE_KEY
    assert "Распоряжение Правительства" in pub.title
    assert "Турецкой Республике" in pub.title
    assert pub.url == "http://publication.pravo.gov.ru/document/0001202608190017"
    assert pub.published_at == dt.datetime(2026, 8, 19, tzinfo=dt.timezone(dt.timedelta(hours=3)))


def test_fetch_documents_uses_ru_proxy_access() -> None:
    with pytest.raises(ValueError, match="RU_PROXY_URL"):
        fetch_documents(ru_proxy_url=None)


# docs/SPEC_pravo_gov_day_by_day.md: обход по календарным дням МСК вместо эскалации
# periodType daily->weekly->monthly (docs/SPEC_pravo_gov_pagination_depth.md) —
# диагностика 02.09 (/tmp/claude_pravo_diag.md) показала, что weekly/monthly на
# pravo.gov.ru — фиксированный текущий период, не окно "последние N дней от now", и не
# достаёт документы старше текущей недели/месяца ни на какой странице пагинации.
NOW = dt.datetime(2026, 8, 28, 20, 0, tzinfo=MOSCOW_TZ)  # 28.08, 20:00 МСК


@pytest.mark.parametrize(
    ("window_start", "now", "expected_days"),
    [
        # обычный ежедневный прогон, окно = 0 — план из одного дня
        (NOW, NOW, [dt.date(2026, 8, 28)]),
        # окно 18 часов, целиком внутри сегодняшнего календарного дня МСК — тоже один день
        (NOW - dt.timedelta(hours=18), NOW, [dt.date(2026, 8, 28)]),
        # окно короче суток (2 часа), но пересекает полночь МСК — план должен включать
        # оба календарных дня, иначе вчерашние публикации не будут запрошены вовсе
        (
            dt.datetime(2026, 8, 27, 23, 0, tzinfo=MOSCOW_TZ),
            dt.datetime(2026, 8, 28, 1, 0, tzinfo=MOSCOW_TZ),
            [dt.date(2026, 8, 27), dt.date(2026, 8, 28)],
        ),
        # ровно сутки назад — два календарных дня
        (NOW - dt.timedelta(days=1), NOW, [dt.date(2026, 8, 27), dt.date(2026, 8, 28)]),
        # длинный простой — план на весь диапазон дней, по возрастанию, без пропусков
        (
            dt.datetime(2026, 8, 24, 10, 0, tzinfo=MOSCOW_TZ),
            dt.datetime(2026, 9, 2, 12, 0, tzinfo=MOSCOW_TZ),
            [
                dt.date(2026, 8, 24),
                dt.date(2026, 8, 25),
                dt.date(2026, 8, 26),
                dt.date(2026, 8, 27),
                dt.date(2026, 8, 28),
                dt.date(2026, 8, 29),
                dt.date(2026, 8, 30),
                dt.date(2026, 8, 31),
                dt.date(2026, 9, 1),
                dt.date(2026, 9, 2),
            ],
        ),
    ],
)
def test_build_day_plan(
    window_start: dt.datetime, now: dt.datetime, expected_days: list[dt.date]
) -> None:
    assert build_day_plan(window_start, now) == expected_days


def test_build_day_plan_converts_non_moscow_tz_to_moscow_calendar_day() -> None:
    # window_start в UTC 23:30 — уже следующий календарный день по МСК (+3), план должен
    # это учитывать, а не брать календарную дату в исходном часовом поясе аргумента.
    window_start = dt.datetime(2026, 8, 27, 23, 30, tzinfo=dt.timezone.utc)  # 28.08, 02:30 МСК
    now = dt.datetime(2026, 8, 28, 6, 0, tzinfo=dt.timezone.utc)  # 28.08, 09:00 МСК

    assert build_day_plan(window_start, now) == [dt.date(2026, 8, 28)]
