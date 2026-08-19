"""Фильтры публикаций перед дальнейшей обработкой, AGENTS.md разделы 4.6 и 13,
PLAN.md Фаза 3.
"""
from __future__ import annotations

from urllib.parse import urlparse

_TEXT_CONTENT_TYPE_PREFIXES = ("text/html", "text/plain", "application/xhtml+xml")
_PDF_CONTENT_TYPE = "application/pdf"


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
