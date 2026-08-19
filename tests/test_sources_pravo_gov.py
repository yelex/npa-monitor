"""Тесты parser/sources/pravo_gov.py на реальной вёрстке /Documents/search
(фрагмент снят вживую 2026-08-19, periodType=weekly)."""
from __future__ import annotations

import datetime as dt

import httpx
import pytest

from parser.sources.pravo_gov import SEARCH_URL, SOURCE_KEY, fetch_documents

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

    publications = fetch_documents(period="weekly", ru_proxy_url="http://proxy.local:8888")

    assert seen_urls == [f"{SEARCH_URL}?block=&periodType=weekly&category=&index=1"]
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
