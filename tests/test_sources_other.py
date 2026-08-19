"""Тесты parser/sources/other.py на реальном формате RSS этих источников
(структура элементов подтверждена вживую 2026-08-20 через feedparser)."""
from __future__ import annotations

import datetime as dt

import httpx
import pytest

from parser.sources.other import FEEDS, fetch_all, fetch_feed

REAL_RIA_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>РИА Новости</title>
<item>
<title>В Прибалтике попытаются помешать россиянам проголосовать на выборах в ГД</title>
<link>https://ria.ru/20260820/mid-2111941007.html</link>
<pubDate>Thu, 20 Aug 2026 00:26:35 +0300</pubDate>
<description>Материал о выборах</description>
</item>
</channel></rss>
"""

REAL_GARANT_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Гарант.ру</title>
<item>
<title>Президент РФ призвал запустить электронную оценку соцучреждений по всей стране</title>
<link>https://www.garant.ru/news/2207299/</link>
<pubDate>Wed, 19 Aug 2026 18:30:00 +0300</pubDate>
<description>Граждане должны иметь возможность выразить мнение о работе школ, больниц</description>
</item>
</channel></rss>
"""


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    real_client_cls = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: real_client_cls(transport=httpx.MockTransport(handler))
    )


def test_fetch_feed_parses_real_rss_item(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == FEEDS["ria.ru"]
        return httpx.Response(200, content=REAL_RIA_RSS.encode(), headers={"content-type": "text/xml; charset=utf-8"})

    _patch_client(monkeypatch, handler)

    publications = fetch_feed("ria.ru")

    assert len(publications) == 1
    pub = publications[0]
    assert pub.source_key == "ria.ru"
    assert "Прибалтике" in pub.title
    assert pub.url == "https://ria.ru/20260820/mid-2111941007.html"
    assert pub.published_at == dt.datetime(2026, 8, 19, 21, 26, 35, tzinfo=dt.timezone.utc)
    assert pub.summary == "Материал о выборах"


def test_fetch_feed_unknown_domain_raises() -> None:
    with pytest.raises(ValueError, match="нет RSS-фида"):
        fetch_feed("unknown.example.com")


def test_fetch_all_skips_unavailable_source_without_crashing(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == FEEDS["garant.ru"]:
            return httpx.Response(
                200, content=REAL_GARANT_RSS.encode(), headers={"content-type": "text/xml; charset=utf-8"}
            )
        raise httpx.ConnectTimeout("boom", request=request)

    _patch_client(monkeypatch, handler)
    from parser.fetcher import _get

    monkeypatch.setattr(_get.retry, "sleep", lambda *_: None)

    results = fetch_all()

    assert set(results) == set(FEEDS)
    assert len(results["garant.ru"]) == 1
    assert results["garant.ru"][0].url == "https://www.garant.ru/news/2207299/"
    for domain in FEEDS:
        if domain != "garant.ru":
            assert results[domain] == []


def test_fetch_all_does_not_swallow_unrelated_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """fetch_all пропускает только SourceUnavailable — баг в парсинге не должен тихо
    прятаться под тем же except, что и «источник недоступен» (AGENTS.md раздел 12)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not valid rss at all", headers={"content-type": "text/xml"})

    _patch_client(monkeypatch, handler)
    monkeypatch.setattr("parser.sources.other._parse_feed", lambda *_: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        fetch_all()
