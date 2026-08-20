"""Тесты parser/sources/kremlin.py на реальной вёрстке kremlin.ru/acts/news
(фрагмент снят вживую 2026-08-19)."""
from __future__ import annotations

import datetime as dt

import httpx
import pytest

from parser.sources._dates import MOSCOW_TZ
from parser.sources.kremlin import NEWS_URL, SOURCE_KEY, fetch_news

REAL_NEWS_FRAGMENT = """
<div class="hentry h-entry hentry_event hentry_doc" data-id="80518" itemscope itemtype="http://schema.org/NewsArticle" role="listitem">
  <h3 class="hentry__title hentry__title_special">
    <a href="/acts/news/80518" itemprop="url" rel="bookmark">
      <span class="entry-title p-name" itemprop="name">Указ о награждении государственными наградами</span>
      <span class="hentry__meta">
        <time class="published dt-published" datetime="2026-08-12" itemprop="datePublished">12 августа 2026 года, 16:30</time>
      </span>
    </a>
  </h3>
</div>
"""


def test_fetch_news_parses_real_markup_fragment(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client_cls = httpx.Client
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(
            200, content=REAL_NEWS_FRAGMENT.encode(), headers={"content-type": "text/html; charset=utf-8"}
        )

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client_cls(transport=httpx.MockTransport(handler)))

    publications = fetch_news(ru_proxy_url="http://proxy.local:8888")

    assert seen_urls == [NEWS_URL]
    assert len(publications) == 1
    pub = publications[0]
    assert pub.source_key == SOURCE_KEY
    assert pub.title == "Указ о награждении государственными наградами"
    assert pub.url == "http://www.kremlin.ru/acts/news/80518"
    # Регрессия: 2026-08-12 без смещения на реальной вёрстке kremlin.ru приводило к
    # naive datetime и падению TypeError при сравнении с tz-aware окном поиска в
    # orchestrator.py — найдено вживую 2026-08-20 (см. parser/sources/_dates.py).
    assert pub.published_at == dt.datetime(2026, 8, 12, tzinfo=MOSCOW_TZ)


def test_fetch_news_page_2_uses_page_path(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client_cls = httpx.Client
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, content=b"<html></html>", headers={"content-type": "text/html"})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client_cls(transport=httpx.MockTransport(handler)))

    fetch_news(page=2, ru_proxy_url="http://proxy.local:8888")

    assert seen_urls == [f"{NEWS_URL}/page/2"]
