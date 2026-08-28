"""Фильтры публикаций перед дальнейшей обработкой, AGENTS.md разделы 4.6 и 13,
PLAN.md Фаза 3.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

_TEXT_CONTENT_TYPE_PREFIXES = ("text/html", "text/plain", "application/xhtml+xml")
_PDF_CONTENT_TYPE = "application/pdf"

# Известные статичные/справочные страницы, ошибочно похожие на свежие публикации
# (дата в самом URL меняется при обходе, контент по сути не датирован) — PLAN.md
# Фаза 9 п.1, docs/SPEC_stale_publications_filter.md. Список, а не жёсткая проверка в
# коде обхода — расширять по мере обнаружения новых паттернов.
_EXCLUDED_URL_PATTERNS = (
    # sfr.gov.ru/branches/<регион>/info/~<дата>/<id> — регионально растиражированные
    # справочные материалы, не публикации о событии (docs/SPEC_stale_publications_filter.md).
    re.compile(r"^https?://(?:www\.)?sfr\.gov\.ru/branches/[^/]+/info/"),
    # vrf.tass.ru — агрегатор региональных СМИ (перепечатки «Популярные новости
    # России», <регион>/<издание>-ru/<id>), не источник публикаций о событии —
    # docs/SPEC_vrf_tass_aggregator_filter.md.
    re.compile(r"^https?://(?:www\.)?vrf\.tass\.ru/"),
)


def is_excluded_path(url: str) -> bool:
    """True, если `url` — известная статичная/справочная страница, не публикация о
    событии (см. `_EXCLUDED_URL_PATTERNS`) — такие публикации не должны создавать сигнал.
    """
    return any(pattern.match(url) for pattern in _EXCLUDED_URL_PATTERNS)


def domain_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_domain_whitelisted(url: str, whitelisted_domains: set[str]) -> bool:
    """AGENTS.md раздел 13: «Передаваемые/принимаемые ссылки проверяются на допустимые
    домены (белый список)». Поддомены белого списка домена считаются допустимыми
    (`www.kremlin.ru` проходит для `kremlin.ru` из справочника).
    """
    host = domain_of(url)
    if not host:
        return False
    return any(host == domain or host.endswith(f".{domain}") for domain in whitelisted_domains)


def is_text_content(content_type: str | None) -> bool:
    """AGENTS.md раздел 4.6: «Только текстовые публикации: новости, анонсы, карточки
    документов. Видео, подкасты, инфографика без текстового описания — не
    обрабатываются». PDF считаем текстовым контентом — `parser/pdf.py` извлекает текст
    (текстовый слой или OCR).
    """
    if not content_type:
        return False
    normalized = content_type.split(";")[0].strip().lower()
    return normalized in _TEXT_CONTENT_TYPE_PREFIXES or normalized == _PDF_CONTENT_TYPE
