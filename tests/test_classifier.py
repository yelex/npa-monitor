"""Тесты parser/classifier.py на синтетических примерах, PLAN.md Фаза 4 (по каждой ЖС,
по каждому уровню приоритета).

Фикстуры используют точные формы ключевых слов из data/*.yaml, а не естественно
согласованные русские предложения — классификатор ищет точные подстроки без
лемматизации/учёта словоформ (известное ограничение MVP, см. AGENTS.md раздел 16).
Тесты отражают реальную работу алгоритма, а не идеализированный текст.
"""
from __future__ import annotations

from db.enums import EventType, Priority, Region, SignalCategory
from parser.classifier import Classifier
from parser.models import Publication

CLASSIFIER = Classifier.load()


def _publication(source_key: str, title: str) -> Publication:
    # url строится из source_key (не независимая фикстура) — detect_region берёт домен
    # из url, а не из source_key (см. parser/classifier.py::detect_region), и все
    # source_key в этом файле уже имеют вид "домен" или "домен/путь".
    return Publication(source_key=source_key, title=title, url=f"https://{source_key}", published_at=None)


# --- По каждой ЖС ---


def test_veterans_publication_is_relevant() -> None:
    pub = _publication("sfr.gov.ru/press_center/news", "постановление ветеран боевых действий выплата")
    result = CLASSIFIER.classify(pub)
    assert result.is_relevant is True
    assert result.categories == (SignalCategory.VETERANS,)


def test_disabled_publication_is_relevant() -> None:
    pub = _publication("mintrud.gov.ru/docs", "приказ инвалид пособие")
    result = CLASSIFIER.classify(pub)
    assert result.is_relevant is True
    assert result.categories == (SignalCategory.DISABLED,)


def test_svo_publication_is_relevant() -> None:
    pub = _publication("tass.ru", "участник СВО компенсация")
    result = CLASSIFIER.classify(pub)
    assert result.is_relevant is True
    assert result.categories == (SignalCategory.SVO,)


def test_multiple_categories_matched_simultaneously() -> None:
    pub = _publication(
        "sfr.gov.ru/press_center/news", "постановление выплата ветеран боевых действий участник СВО"
    )
    result = CLASSIFIER.classify(pub)
    assert set(result.categories) == {SignalCategory.VETERANS, SignalCategory.SVO}


# --- По каждому уровню приоритета (раздел 7 AGENTS.md) ---


def test_high_priority_document_marker_priority_word_and_known_region() -> None:
    pub = _publication(
        "sfr.gov.ru/press_center/news", "постановление ветеран боевых действий выплата новый"
    )
    result = CLASSIFIER.classify(pub)
    assert result.region == Region.RF
    assert result.priority == Priority.HIGH


def test_medium_priority_document_marker_without_priority_word() -> None:
    pub = _publication("mintrud.gov.ru/docs", "приказ инвалид пособие")
    result = CLASSIFIER.classify(pub)
    assert result.region == Region.RF
    assert result.priority == Priority.MEDIUM


def test_medium_priority_forced_by_undefined_region_even_with_high_words() -> None:
    """AGENTS.md раздел 4.1: регион «Не определён» -> средний приоритет, даже если
    остальные признаки указывали бы на высокий."""
    pub = _publication("tass.ru", "постановление ветеран боевых действий выплата новый")
    result = CLASSIFIER.classify(pub)
    assert result.region == Region.UNDEFINED
    assert result.priority == Priority.MEDIUM


def test_low_priority_without_document_marker() -> None:
    pub = _publication("tass.ru", "участник СВО компенсация")
    result = CLASSIFIER.classify(pub)
    assert result.priority == Priority.LOW


# --- Регион ---


def test_moscow_region_detected_from_regional_source() -> None:
    pub = _publication("mos.ru/authority/documents", "постановление участник СВО выплата")
    result = CLASSIFIER.classify(pub)
    assert result.region == Region.MOSCOW


def test_undefined_region_for_contextual_source() -> None:
    pub = _publication("tass.ru", "постановление участник СВО выплата")
    result = CLASSIFIER.classify(pub)
    assert result.region == Region.UNDEFINED


# --- Релевантность ---


def test_not_relevant_without_life_situation_keyword() -> None:
    pub = _publication("sfr.gov.ru/press_center/news", "постановление выплата новый")
    result = CLASSIFIER.classify(pub)
    assert result.is_relevant is False


def test_not_relevant_without_topic_block_keyword() -> None:
    pub = _publication("sfr.gov.ru/press_center/news", "ветеран боевых действий постановление новый")
    result = CLASSIFIER.classify(pub)
    assert result.is_relevant is False


# --- Тип события ---


def test_event_type_repeal_detected() -> None:
    pub = _publication(
        "sfr.gov.ru/press_center/news", "постановление ветеран боевых действий выплата утратил силу"
    )
    assert CLASSIFIER.classify(pub).event_type == EventType.REPEAL


def test_event_type_entry_into_force_detected() -> None:
    pub = _publication(
        "sfr.gov.ru/press_center/news", "постановление ветеран боевых действий выплата вступил в силу"
    )
    assert CLASSIFIER.classify(pub).event_type == EventType.ENTRY_INTO_FORCE


def test_event_type_amendment_detected() -> None:
    pub = _publication(
        "sfr.gov.ru/press_center/news", "постановление ветеран боевых действий выплата внесены изменения"
    )
    assert CLASSIFIER.classify(pub).event_type == EventType.AMENDMENT


def test_event_type_new_document_detected() -> None:
    pub = _publication(
        "sfr.gov.ru/press_center/news", "постановление ветеран боевых действий выплата принят"
    )
    assert CLASSIFIER.classify(pub).event_type == EventType.NEW_DOCUMENT


def test_event_type_defaults_to_review_without_markers() -> None:
    pub = _publication("sfr.gov.ru/press_center/news", "постановление ветеран боевых действий выплата")
    assert CLASSIFIER.classify(pub).event_type == EventType.REVIEW


# --- explain() / трейс ---


def test_explain_result_matches_classify() -> None:
    pub = _publication("sfr.gov.ru/press_center/news", "постановление ветеран боевых действий выплата")
    assert CLASSIFIER.explain(pub).result == CLASSIFIER.classify(pub)


def test_explain_reports_matched_keywords() -> None:
    pub = _publication("mintrud.gov.ru/docs", "приказ инвалид пособие")
    trace = CLASSIFIER.explain(pub)

    assert trace.category_matches == {SignalCategory.DISABLED: ("инвалид",)}
    assert trace.topic_block_matches == ("пособие",)
    assert trace.document_marker_matches == ("приказ",)


def test_explain_reports_no_matches_when_irrelevant() -> None:
    pub = _publication("sfr.gov.ru/press_center/news", "постановление выплата новый")
    trace = CLASSIFIER.explain(pub)

    assert trace.category_matches == {}
    assert trace.result.is_relevant is False


def test_explain_format_is_readable() -> None:
    pub = _publication("mintrud.gov.ru/docs", "приказ инвалид пособие")
    text = CLASSIFIER.explain(pub).format()

    assert "инвалид" in text
    assert "пособие" in text
    assert "РЕЛЕВАНТНО" in text
