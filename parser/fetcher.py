"""Общий HTTP-клиент парсера, PLAN.md Фаза 3.

Ретраи (3 попытки → `SourceUnavailable`, AGENTS.md раздел 12 «Источник недоступен»),
автоопределение кодировки для доменов без charset в заголовке (`garant.ru`,
`pravo.gov.ru` отдают cp1251 без объявления — docs/STAGE0.md), и схема прямой-запрос/
через-`RU_PROXY_URL` в зависимости от `db.catalog.Source.access` (AGENTS.md раздел 4,
docs/STAGE0.md раздел 2.1 — RSNET режет не-РФ IP на 443/80 напрямую).
"""
from __future__ import annotations

import dataclasses

import charset_normalizer
import httpx
import tenacity

DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
# Браузероподобный UA: идентифицирующий UA ("npa-monitor/0.1 ...") получает 403 от
# mintrud.gov.ru (проверено вживую при разработке parser/sources/mintrud.py) — это WAF-
# эвристика по виду UA, не запрет в robots.txt (там разрешено для User-agent: *, путь
# /docs не в Disallow). sfr.gov.ru пропускает оба варианта UA.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}

_RETRYABLE_EXCEPTIONS = (httpx.TransportError, httpx.HTTPStatusError)


@dataclasses.dataclass(frozen=True)
class FetchResult:
    url: str
    status_code: int
    text: str
    content_type: str | None


class SourceUnavailable(Exception):
    """Источник не ответил за 3 попытки — AGENTS.md раздел 12, пропуск до следующего цикла."""

    def __init__(self, url: str, access: str, last_error: BaseException) -> None:
        super().__init__(f"{url} (access={access}) недоступен после 3 попыток: {last_error!r}")
        self.url = url
        self.access = access
        self.last_error = last_error


def _decode(content: bytes, declared_encoding: str | None) -> str:
    """`apparent_encoding`-приём из measure_deepagent (docs/STAGE0.md, раздел 1).

    Если сервер не объявил charset (или объявленный не подходит) — определяем эвристикой
    (`charset_normalizer`), а не молча портим кириллицу как cp1251-в-utf-8.
    """
    if declared_encoding:
        try:
            return content.decode(declared_encoding)
        except (LookupError, UnicodeDecodeError):
            pass

    best_match = charset_normalizer.from_bytes(content).best()
    if best_match is not None:
        return str(best_match)
    return content.decode("utf-8", errors="replace")


def _client_kwargs(access: str, ru_proxy_url: str | None) -> dict:
    if access == "direct":
        return {}
    if access == "ru_proxy":
        if not ru_proxy_url:
            raise ValueError(
                "access='ru_proxy' требует RU_PROXY_URL (см. .env.example, docs/STAGE0.md раздел 2.1)"
            )
        return {"proxy": ru_proxy_url}
    raise ValueError(
        f"access={access!r} не поддерживается fetcher'ом (только 'direct'/'ru_proxy'; "
        f"'unsupported' — источник не обходится автоматически, см. data/*.yaml)"
    )


@tenacity.retry(
    stop=tenacity.stop_after_attempt(3),
    wait=tenacity.wait_exponential(multiplier=1, min=1, max=10),
    retry=tenacity.retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
    reraise=True,
)
def _get(client: httpx.Client, url: str) -> httpx.Response:
    response = client.get(url, timeout=DEFAULT_TIMEOUT, headers=DEFAULT_HEADERS)
    response.raise_for_status()
    return response


def fetch(url: str, *, access: str, ru_proxy_url: str | None = None) -> FetchResult:
    """Скачивает `url` схемой, заданной `access` (`db.catalog.Source.access`).

    После 3 неудачных попыток поднимает `SourceUnavailable` — вызывающий код (state.py,
    оркестратор обхода) должен поймать её и пропустить источник до следующего цикла, не
    прерывая обход остальных источников.
    """
    client_kwargs = _client_kwargs(access, ru_proxy_url)
    try:
        with httpx.Client(follow_redirects=True, **client_kwargs) as client:
            response = _get(client, url)
    except _RETRYABLE_EXCEPTIONS as exc:
        raise SourceUnavailable(url=url, access=access, last_error=exc) from exc

    text = _decode(response.content, response.charset_encoding)
    return FetchResult(
        url=url,
        status_code=response.status_code,
        text=text,
        content_type=response.headers.get("content-type"),
    )
