"""LLM-fallback для классификатора (GLM), AGENTS.md раздел 5 и раздел 16 п.9;
PLAN.md Фаза 4.

Не обязателен для MVP — AGENTS.md раздел 5: «LLM закладывается как опциональный второй
проход для узких мест с низким покрытием regex-правил» (в первую очередь дата
вступления в силу, `parser/effective_date.py`). Интерфейс `ClassifierLLMClient` +
реализация `GLMClient` — тонкая обёртка над OpenAI-совместимым API GLM (api.z.ai), без
LangChain: единственный нужный вызов — один запрос/один ответ, тяжёлая зависимость
(`langchain-openai`+`langchain-core`+`openai`) не оправдана для этого.

Креды — `.env` (`GLM_PERSONAL_API_KEY`/`GLM_PERSONAL_BASE_URL`/`GLM_PERSONAL_MODEL`),
паттерн вызова взят из `/Users/user/dev/auto/eval/llm_factory.py::get_glm_personal()`
(AGENTS.md раздел 16, п.9), значения переменных не коммитятся.
"""
from __future__ import annotations

import os
from typing import Protocol

import httpx

DEFAULT_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
DEFAULT_MODEL = "glm-5.1"


class LLMError(Exception):
    """LLM недоступен/вернул неожиданный ответ.

    Вызывающий код (Фаза 4/6) должен ловить эту ошибку и просто пропускать
    LLM-уточнение, не прерывая основной regex-путь классификации — LLM здесь строго
    опциональный второй проход, а не обязательное звено пайплайна.
    """


class ClassifierLLMClient(Protocol):
    """Абстракция над LLM-провайдером для узких мест классификации (Фаза 4)."""

    def complete(self, prompt: str) -> str:
        """Текстовый ответ модели на `prompt`. Поднимает `LLMError` при сбое."""
        ...


class GLMClient:
    """OpenAI-совместимый клиент GLM (api.z.ai coding plan)."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._timeout = timeout

    def complete(self, prompt: str) -> str:
        try:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(
                    f"{self._base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={
                        "model": self._model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.1,
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMError(f"вызов GLM не удался: {exc!r}") from exc

        data = response.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"неожиданный формат ответа GLM: {data!r}") from exc


def get_default_client() -> GLMClient | None:
    """`None`, если GLM не сконфигурирован (`GLM_PERSONAL_API_KEY` не задан в `.env`) —
    вызывающий код должен трактовать это как «LLM-уточнение недоступно», не как ошибку.
    """
    api_key = os.getenv("GLM_PERSONAL_API_KEY")
    if not api_key:
        return None
    return GLMClient(
        api_key=api_key,
        base_url=os.getenv("GLM_PERSONAL_BASE_URL", DEFAULT_BASE_URL),
        model=os.getenv("GLM_PERSONAL_MODEL", DEFAULT_MODEL),
    )
