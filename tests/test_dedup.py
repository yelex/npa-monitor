"""Тесты parser/dedup.py, PLAN.md Фаза 9 п.2, docs/SPEC_url_canonicalization.md,
docs/SPEC_content_dedup.md."""
from __future__ import annotations

from parser.dedup import (
    canonicalize_url,
    find_duplicate_title,
    normalize_title,
    title_similarity,
)
from parser.llm import LLMError


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


# --- вторичный слой дедупа по содержанию (docs/SPEC_content_dedup.md) ---


class _StubLLMClient:
    def __init__(self, answer: str) -> None:
        self._answer = answer
        self.calls: list[str] = []

    def complete(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self._answer


class _FailingLLMClient:
    def complete(self, prompt: str) -> str:
        raise LLMError("недоступен")


TITLE = "Ветераны боевых действий столичного региона смогут быстрее оформить льготы"


def test_normalize_title_ignores_case_and_punctuation() -> None:
    assert normalize_title("Принят Закон №105н!") == normalize_title("принят закон 105н")


def test_title_similarity_identical_titles_is_one() -> None:
    assert title_similarity(TITLE, TITLE) == 1.0


def test_title_similarity_unrelated_titles_is_low() -> None:
    other = "В Оренбуржье выловили крупного сазана на рыболовном фестивале"
    assert title_similarity(TITLE, other) < 0.2


def test_find_duplicate_title_exact_match_without_llm_client() -> None:
    existing = [(1, TITLE, 42)]

    match = find_duplicate_title(TITLE, existing, llm_client=None)

    assert match == (1, 42)


def test_find_duplicate_title_ignores_unrelated_titles() -> None:
    existing = [(1, "В Оренбуржье выловили крупного сазана на рыболовном фестивале", None)]

    assert find_duplicate_title(TITLE, existing, llm_client=None) is None


def test_find_duplicate_title_borderline_case_without_llm_client_is_not_a_duplicate() -> None:
    # Похожий, но не дословно совпадающий заголовок — без LLM пограничные случаи не
    # считаются дублями (докстринг find_duplicate_title, docs/SPEC_content_dedup.md).
    paraphrased = "Столичные ветераны боевых действий смогут быстрее оформить льготы"
    existing = [(1, paraphrased, 42)]

    assert find_duplicate_title(TITLE, existing, llm_client=None) is None


def test_find_duplicate_title_borderline_case_asks_llm_and_accepts_yes() -> None:
    paraphrased = "Столичные ветераны боевых действий смогут быстрее оформить льготы"
    existing = [(1, paraphrased, 42)]
    llm = _StubLLMClient("ДА, это одна и та же публикация")

    match = find_duplicate_title(TITLE, existing, llm_client=llm)

    assert match == (1, 42)
    assert len(llm.calls) == 1


def test_find_duplicate_title_borderline_case_respects_llm_no() -> None:
    paraphrased = "Столичные ветераны боевых действий смогут быстрее оформить льготы"
    existing = [(1, paraphrased, 42)]
    llm = _StubLLMClient("Нет")

    assert find_duplicate_title(TITLE, existing, llm_client=llm) is None


def test_find_duplicate_title_llm_error_degrades_to_not_a_duplicate() -> None:
    paraphrased = "Столичные ветераны боевых действий смогут быстрее оформить льготы"
    existing = [(1, paraphrased, 42)]

    assert find_duplicate_title(TITLE, existing, llm_client=_FailingLLMClient()) is None


def test_find_duplicate_title_does_not_call_llm_for_unrelated_candidate() -> None:
    existing = [(1, "В Оренбуржье выловили крупного сазана на рыболовном фестивале", None)]
    llm = _StubLLMClient("ДА")

    assert find_duplicate_title(TITLE, existing, llm_client=llm) is None
    assert llm.calls == []  # заведомо разные — LLM не вызывается
