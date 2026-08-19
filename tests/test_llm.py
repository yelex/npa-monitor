"""Тесты parser/llm.py. Без реального обращения к GLM — httpx.MockTransport."""
from __future__ import annotations

import httpx
import pytest

from parser.llm import GLMClient, LLMError, get_default_client


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    real_client_cls = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: real_client_cls(transport=httpx.MockTransport(handler), **kwargs)
    )


def test_complete_returns_message_content(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        assert str(request.url) == "https://api.z.ai/api/coding/paas/v4/chat/completions"
        return httpx.Response(200, json={"choices": [{"message": {"content": "2027-01-01"}}]})

    _patch_client(monkeypatch, handler)
    client = GLMClient(api_key="test-key")

    result = client.complete("Когда документ вступает в силу?")

    assert result == "2027-01-01"


def test_complete_raises_llm_error_on_http_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    _patch_client(monkeypatch, handler)
    client = GLMClient(api_key="test-key")

    with pytest.raises(LLMError, match="вызов GLM не удался"):
        client.complete("prompt")


def test_complete_raises_llm_error_on_unexpected_response_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    _patch_client(monkeypatch, handler)
    client = GLMClient(api_key="test-key")

    with pytest.raises(LLMError, match="неожиданный формат ответа"):
        client.complete("prompt")


def test_get_default_client_returns_none_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GLM_PERSONAL_API_KEY", raising=False)

    assert get_default_client() is None


def test_get_default_client_returns_configured_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLM_PERSONAL_API_KEY", "secret")
    monkeypatch.setenv("GLM_PERSONAL_MODEL", "glm-custom")

    client = get_default_client()

    assert client is not None
    assert client._api_key == "secret"
    assert client._model == "glm-custom"
