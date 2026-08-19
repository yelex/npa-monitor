"""Тесты parser/sources/government.py на реальной вёрстке government.ru/docs/
(фрагмент снят вживую 2026-08-19)."""
from __future__ import annotations

import datetime as dt

import httpx
import pytest

from parser.sources.government import DOCS_URL, SOURCE_KEY, fetch_docs

REAL_DOCS_FRAGMENT = """
<div class="feed_content ajax-paginator-page">
<div class="headline" data-id="59616">
  <span class="headline_date">
    <time datetime="2026-08-19T10:00:00+04:00">19 августа 2026</time>,
    <a href="/rugovclassifier/905/">Комплексная государственная программа «Строительство»</a>
  </span>
  <a class="headline__link open-reader-js" data-ajax-url="/docs/59616/?ajax=reader" href="/docs/59616/">
    <span class="headline_title"><span class="headline_title_link">Правительство расширило перечень мероприятий, реализуемых в рамках государственной программы «Строительство»</span></span>
    <span class="headline_lead">Распоряжение от 18 августа 2026 года №2194-р</span>
  </a>
</div>
</div>
"""


def test_fetch_docs_parses_real_markup_fragment(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client_cls = httpx.Client
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(
            200, content=REAL_DOCS_FRAGMENT.encode(), headers={"content-type": "text/html; charset=utf-8"}
        )

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client_cls(transport=httpx.MockTransport(handler)))

    publications = fetch_docs(ru_proxy_url="http://proxy.local:8888")

    assert seen_urls == [DOCS_URL]
    assert len(publications) == 1
    pub = publications[0]
    assert pub.source_key == SOURCE_KEY
    assert "государственной программы «Строительство»" in pub.title
    assert pub.url == "http://government.ru/docs/59616/"
    assert pub.published_at == dt.datetime(2026, 8, 19, 10, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=4)))
    assert pub.summary == "Распоряжение от 18 августа 2026 года №2194-р"


def test_fetch_docs_page_2_appends_query_param(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client_cls = httpx.Client
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, content=b"<html></html>", headers={"content-type": "text/html"})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client_cls(transport=httpx.MockTransport(handler)))

    fetch_docs(page=2, ru_proxy_url="http://proxy.local:8888")

    assert seen_urls == [f"{DOCS_URL}?page=2"]
