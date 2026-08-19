"""Тесты parser/sources/sfr.py на реальной вёрстке sfr.gov.ru/press_center/news/
(фрагмент снят вживую 2026-08-19 при разработке адаптера, docs/STAGE0.md раздел 2)."""
from __future__ import annotations

import datetime as dt

import httpx
import pytest

from parser.sources.sfr import NEWS_URL, SOURCE_KEY, fetch_news

REAL_NEWS_FRAGMENT = """
<html><body><main>
<article class="re-news__article">
  <div class="re-news__article-content">
    <div class="re-news__article-info">
      <time class="re-news__article-time" datetime="2026-08-19T16:36:57+03:00">
        <span class="date re-news__article-date">19 августа 2026</span>
        <span class="time d-inline-block">16:36</span>
      </time>
    </div>
    <a class="re-news__article-link" href="/press_center/news/~2026/08/19/284025">
      <h3 class="re-news__article-title">Соцфонд направил россиянам 4,7 миллиона уведомлений о положенных мерах поддержки</h3>
    </a>
    <p class="re-news__article-description">C января граждане получили 4,7 млн сообщений о выплатах и услугах</p>
  </div>
</article>
<article class="re-news__article">
  <div class="re-news__article-content">
    <div class="re-news__article-info">
      <time class="re-news__article-time" datetime="2026-08-18T12:17:50+03:00">
        <span class="date re-news__article-date">18 августа 2026</span>
        <span class="time d-inline-block">12:17</span>
      </time>
    </div>
    <a class="re-news__article-link" href="/press_center/news/~2026/08/18/283992">
      <h3 class="re-news__article-title">Более 15 тысяч бойцов СВО прошли лечение в центрах Социального фонда</h3>
    </a>
    <p class="re-news__article-description">Ветераны могут приезжать в реабилитационный центр вместе с сопровождающим</p>
  </div>
</article>
</main></body></html>
"""


def test_fetch_news_parses_real_markup_fragment(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client_cls = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == NEWS_URL
        return httpx.Response(200, content=REAL_NEWS_FRAGMENT.encode(), headers={"content-type": "text/html; charset=utf-8"})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client_cls(transport=httpx.MockTransport(handler)))

    publications = fetch_news()

    assert len(publications) == 2
    first, second = publications
    assert first.source_key == SOURCE_KEY
    assert first.title == "Соцфонд направил россиянам 4,7 миллиона уведомлений о положенных мерах поддержки"
    assert first.url == "https://sfr.gov.ru/press_center/news/~2026/08/19/284025"
    assert first.published_at == dt.datetime(2026, 8, 19, 16, 36, 57, tzinfo=dt.timezone(dt.timedelta(hours=3)))
    assert "СВО" in second.title


def test_fetch_news_page_2_appends_query_param(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client_cls = httpx.Client
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, content=b"<html><body></body></html>", headers={"content-type": "text/html"})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client_cls(transport=httpx.MockTransport(handler)))

    fetch_news(page=2)

    assert seen_urls == [f"{NEWS_URL}?page=2"]
