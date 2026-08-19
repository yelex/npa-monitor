"""Тесты parser/fetcher.py, PLAN.md Фаза 3. Без реальной сети — httpx.MockTransport."""
from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from parser.fetcher import SourceUnavailable, _get, fetch


def _patch_client(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    real_client_cls = httpx.Client

    def fake_client(**kwargs: object) -> httpx.Client:
        return real_client_cls(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "Client", fake_client)


RUSSIAN_SAMPLE_TEXT = (
    "Постановление Правительства Российской Федерации о мерах поддержки участников "
    "специальной военной операции и членов их семей вступает в силу со дня официального "
    "опубликования на портале правовой информации."
)


def test_fetch_direct_decodes_declared_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=RUSSIAN_SAMPLE_TEXT.encode(),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    _patch_client(monkeypatch, handler)

    result = fetch("https://sfr.gov.ru/", access="direct")

    assert result.status_code == 200
    assert result.text == RUSSIAN_SAMPLE_TEXT


def test_fetch_falls_back_to_charset_normalizer_when_encoding_not_declared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # garant.ru/pravo.gov.ru отдают cp1251 без charset в заголовке (docs/STAGE0.md).
        # Короткие строки детектор кодировки угадывает ненадёжно — берём длинный
        # реалистичный русский текст, как в реальных публикациях.
        return httpx.Response(
            200,
            content=RUSSIAN_SAMPLE_TEXT.encode("cp1251"),
            headers={"content-type": "text/html"},
        )

    _patch_client(monkeypatch, handler)

    result = fetch("https://garant.ru/", access="direct")

    assert "специальной военной операции" in result.text


def test_fetch_ru_proxy_requires_proxy_url() -> None:
    with pytest.raises(ValueError, match="RU_PROXY_URL"):
        fetch("http://publication.pravo.gov.ru/", access="ru_proxy", ru_proxy_url=None)


def test_fetch_rejects_unsupported_access() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        fetch("https://dszn.ru/", access="unsupported")


def test_fetch_raises_source_unavailable_after_retries_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectTimeout("boom", request=request)

    _patch_client(monkeypatch, handler)
    monkeypatch.setattr(_get.retry, "sleep", lambda *_: None)  # без реальных пауз между попытками

    with pytest.raises(SourceUnavailable) as exc_info:
        fetch("http://www.kremlin.ru/", access="direct")

    assert attempts == 3
    assert exc_info.value.url == "http://www.kremlin.ru/"
