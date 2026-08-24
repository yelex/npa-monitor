"""Тесты parser/dedup.py, PLAN.md Фаза 9 п.2, docs/SPEC_url_canonicalization.md."""
from __future__ import annotations

from parser.dedup import canonicalize_url


def test_canonicalize_url_strips_www() -> None:
    assert canonicalize_url("https://www.rg.ru/2026/08/21/x.html") == canonicalize_url(
        "https://rg.ru/2026/08/21/x.html"
    )


def test_canonicalize_url_normalizes_scheme_to_https() -> None:
    assert canonicalize_url("http://publication.pravo.gov.ru/document/1") == canonicalize_url(
        "https://publication.pravo.gov.ru/document/1"
    )


def test_canonicalize_url_strips_noisy_query_params() -> None:
    base = canonicalize_url("http://publication.pravo.gov.ru/document/3401202608200009")
    assert canonicalize_url("http://publication.pravo.gov.ru/document/3401202608200009?index=9") == base
    assert canonicalize_url("http://publication.pravo.gov.ru/document/3401202608200009?index=10") == base


def test_canonicalize_url_keeps_meaningful_query_params() -> None:
    a = canonicalize_url("https://example.com/doc?document_id=1")
    b = canonicalize_url("https://example.com/doc?document_id=2")
    assert a != b


def test_canonicalize_url_sorts_remaining_query_params_for_stable_order() -> None:
    a = canonicalize_url("https://example.com/doc?b=2&a=1")
    b = canonicalize_url("https://example.com/doc?a=1&b=2")
    assert a == b


def test_canonicalize_url_strips_trailing_slash_and_fragment() -> None:
    a = canonicalize_url("https://example.com/doc/")
    b = canonicalize_url("https://example.com/doc#section")
    assert a == b == "https://example.com/doc"


def test_canonicalize_url_does_not_merge_different_paths_on_same_domain() -> None:
    # Известное ограничение (docs/SPEC_url_canonicalization.md, раздел 3):
    # minjust.consultant.ru/documents/60711 vs .../special/documents/document/60711
    # — тот же документ, разные пути, не схлопывается канонизацией URL.
    a = canonicalize_url("https://minjust.consultant.ru/documents/60711?items=1")
    b = canonicalize_url("https://minjust.consultant.ru/special/documents/document/60711?items=1")
    assert a != b
