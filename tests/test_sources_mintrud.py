"""Тесты parser/sources/mintrud.py на реальной вёрстке mintrud.gov.ru/docs
(фрагмент снят вживую 2026-08-19 при разработке адаптера)."""
from __future__ import annotations

import datetime as dt

import httpx
import pytest

from parser.sources.mintrud import DOCS_URL, SOURCE_KEY, fetch_docs

REAL_DOCS_FRAGMENT = """
<html><body><div class="js-documents-container">
<div class="post-list">
  <p class="page-date text-light">24 июля 2026</p>
  <a class="text-black" href="/docs/2810">
    <p class="post-name">Отчет о ходе реализации государственной программы Российской Федерации «Доступная среда» за 2025 год</p>
    <p>Отчет за 2025 год</p>
  </a>
</div>
<div class="post-list">
  <p class="page-date text-light">24 июля 2026</p>
  <a class="text-black" href="/docs/mintrud/orders/3223">
    <p class="post-name">Приказ Минтруда России № 312 от 24 июля 2026 г.</p>
    <p>О внесении изменений в приложение к приказу Министерства труда и социальной защиты Российской Федерации от 11 декабря 2025 г. № 700</p>
  </a>
</div>
</div></body></html>
"""


def test_fetch_docs_parses_real_markup_fragment(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client_cls = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == DOCS_URL
        return httpx.Response(200, content=REAL_DOCS_FRAGMENT.encode(), headers={"content-type": "text/html; charset=utf-8"})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client_cls(transport=httpx.MockTransport(handler)))

    publications = fetch_docs()

    assert len(publications) == 2
    first, second = publications
    assert first.source_key == SOURCE_KEY
    assert "Доступная среда" in first.title
    assert first.url == "https://mintrud.gov.ru/docs/2810"
    assert first.published_at == dt.datetime(2026, 7, 24, tzinfo=dt.timezone.utc)
    assert second.url == "https://mintrud.gov.ru/docs/mintrud/orders/3223"
    assert "312" in second.title


def test_fetch_docs_page_2_uses_directory_id_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client_cls = httpx.Client
    seen_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, content=b"<html><body></body></html>", headers={"content-type": "text/html"})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client_cls(transport=httpx.MockTransport(handler)))

    fetch_docs(page=2)

    assert seen_urls == [f"{DOCS_URL}?directoryId=128&page=2"]
