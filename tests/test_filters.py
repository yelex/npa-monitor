"""Тесты parser/filters.py, PLAN.md Фаза 3."""
from __future__ import annotations

from parser.filters import is_domain_whitelisted, is_text_content

WHITELIST = {"kremlin.ru", "sfr.gov.ru", "mos.ru"}


def test_is_domain_whitelisted_exact_match() -> None:
    assert is_domain_whitelisted("http://kremlin.ru/acts/news/80518", WHITELIST) is True


def test_is_domain_whitelisted_subdomain_matches() -> None:
    assert is_domain_whitelisted("https://www.mos.ru/authority/documents/doc/1/", WHITELIST) is True
    assert is_domain_whitelisted("https://msupport.dszn.ru/news", WHITELIST) is False


def test_is_domain_whitelisted_rejects_unlisted_domain() -> None:
    assert is_domain_whitelisted("https://evil.example.com/npa", WHITELIST) is False


def test_is_domain_whitelisted_rejects_lookalike_domain() -> None:
    # "not-kremlin.ru" не должен матчиться под "kremlin.ru" через endswith без точки
    assert is_domain_whitelisted("https://not-kremlin.ru/", {"kremlin.ru"}) is False


def test_is_domain_whitelisted_empty_url_is_false() -> None:
    assert is_domain_whitelisted("", WHITELIST) is False


def test_is_text_content_accepts_html_and_pdf() -> None:
    assert is_text_content("text/html; charset=utf-8") is True
    assert is_text_content("application/pdf") is True
    assert is_text_content("text/plain") is True


def test_is_text_content_rejects_video_and_missing_header() -> None:
    assert is_text_content("video/mp4") is False
    assert is_text_content(None) is False
    assert is_text_content("") is False
