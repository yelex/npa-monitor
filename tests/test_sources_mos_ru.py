"""Тесты parser/sources/mos_ru.py на реальной структуре __NEXT_DATA__
(снята вживую 2026-08-19 с mos.ru/authority/documents/)."""
from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from parser.sources.mos_ru import DOCUMENTS_URL, SOURCE_KEY, fetch_documents

REAL_NEXT_DATA = {
    "props": {
        "pageProps": {
            "initialState": {
                "documentsList": {
                    "data": [
                        {
                            "id": 58844220,
                            "title": (
                                "Распоряжение № 64626 от 17.08.2026 "
                                "«Об изъятии для государственных нужд объектов "
                                "недвижимого имущества»     "
                            ),
                            "number": "64626",
                            "date_published": "2026-08-19 11:47:00",
                        },
                        {
                            "id": 58844111,
                            "title": "Без даты публикации",
                            "number": "1",
                            "date_published": None,
                        },
                    ],
                    "meta": {"totalCount": 5851, "currentPage": 1},
                }
            }
        }
    }
}

REAL_DOCUMENTS_PAGE = f"""
<html><body>
<script id="__NEXT_DATA__" type="application/json">{json.dumps(REAL_NEXT_DATA)}</script>
</body></html>
"""


def test_fetch_documents_parses_next_data_json(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client_cls = httpx.Client
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(
            200, content=REAL_DOCUMENTS_PAGE.encode(), headers={"content-type": "text/html; charset=utf-8"}
        )

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client_cls(transport=httpx.MockTransport(handler)))

    publications = fetch_documents()

    assert seen_urls == [DOCUMENTS_URL]
    assert len(publications) == 2

    first, second = publications
    assert first.source_key == SOURCE_KEY
    assert "Распоряжение № 64626" in first.title
    assert first.url == f"{DOCUMENTS_URL}doc/58844220/"
    assert first.published_at == dt.datetime(2026, 8, 19, 11, 47, 0, tzinfo=dt.timezone(dt.timedelta(hours=3)))

    assert second.published_at is None


def test_fetch_documents_returns_empty_list_when_next_data_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client_cls = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html><body>no data here</body></html>", headers={"content-type": "text/html"})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client_cls(transport=httpx.MockTransport(handler)))

    assert fetch_documents() == []
