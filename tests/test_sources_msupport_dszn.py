"""Тесты parser/sources/msupport_dszn.py на реальной вёрстке msupport.dszn.ru/news
(фрагмент снят вживую 2026-08-19)."""
from __future__ import annotations

import datetime as dt

import httpx
import pytest

from parser.sources.msupport_dszn import NEWS_URL, SOURCE_KEY, fetch_news

REAL_NEWS_FRAGMENT = """
<a href="https://dszn.ru/press-center/news/14264" target="_black">
  <div class="img-wrap">
    <img alt="" class="img-fluid" src="/assets/x.jpg" />
  </div>
  <span class="date">14 Августа 2026 года</span>
  <p><b>Более 2,5 тысячи московских школьников из семей участников СВО приняли участие в профориентационном проекте «Стажировки» за четыре года</b></p>
</a>
<a href="https://dszn.ru/press-center/news/14253" target="_black">
  <div class="img-wrap">
    <img alt="" class="img-fluid" src="/assets/y.jpg" />
  </div>
  <span class="date">6 Августа 2026 года</span>
  <p><b>В Москве запустили онлайн-консультации по мерам поддержки для участников СВО и их семей</b></p>
</a>
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

    publications = fetch_news()

    assert seen_urls == [NEWS_URL]
    assert len(publications) == 2
    first, second = publications
    assert first.source_key == SOURCE_KEY
    assert "профориентационном проекте" in first.title
    assert first.url == "https://dszn.ru/press-center/news/14264"
    assert first.published_at == dt.datetime(2026, 8, 14, tzinfo=dt.timezone.utc)
    assert second.published_at == dt.datetime(2026, 8, 6, tzinfo=dt.timezone.utc)


def test_fetch_news_page_2_uses_page_path(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client_cls = httpx.Client
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, content=b"<html></html>", headers={"content-type": "text/html"})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client_cls(transport=httpx.MockTransport(handler)))

    fetch_news(page=2)

    assert seen_urls == [f"{NEWS_URL}/page-2"]
