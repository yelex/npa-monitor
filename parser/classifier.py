"""Классификатор публикаций: релевантность, категория (ЖС), тип события, регион,
приоритет — AGENTS.md разделы 4, 5, 7; PLAN.md Фаза 4.

MVP — детерминированные keyword-правила, без LLM в основном контуре (AGENTS.md
раздел 5, «Классификация»). Ключевые слова — из справочников `data/` (`db/catalog.py`),
не хардкод в этом модуле.

Решения, где формулировка AGENTS.md допускала неоднозначность (задокументировано, чтобы
не потерялось — пересмотреть по итогам пилота, AGENTS.md раздел 16):

- **Релевантность** (раздел 4.4) — строго по букве: ЖС-ключевое слово И слово
  тематического блока (5.2). Маркеры документа (5.3) релевантность не решают, только
  участвуют в приоритизации (см. ниже) — раздел 4.4 явно перечисляет только 2 условия.
- **Тип события** — прямого маппинга «ключевое слово → `EventType`» в AGENTS.md нет
  (раздел 5.4 «маркеры сути» — один общий список без разбивки по типам); разбивка по
  `data/keywords.yaml::event_type_markers` составлена реализатором.
- **Приоритизация** (раздел 7) — «есть ссылка на документ» трактуется как «в тексте
  есть маркер документа» (5.3), а не как «есть URL публикации» (URL есть всегда, иначе
  не было бы `Publication`). Без маркера документа — низкий приоритет (раздел 7:
  «аналитика, обзор... без указания на конкретный документ»). Регион «Не определён» —
  принудительно средний приоритет (раздел 4.1), даже если остальные признаки указывали
  бы на высокий.

Сопоставление ключевых слов — через `parser/ru_stem.py` (допуск на падежные/числовые
словоформы, не точная подстрока) — см. докстринг того модуля: точное совпадение
пропускало реальные релевантные публикации (проверено вживую 2026-08-20).
"""
from __future__ import annotations

import dataclasses

from db.catalog import (
    ClassificationKeywords,
    LifeSituation,
    RegionEntry,
    load_classification_keywords,
    load_life_situations,
    load_regions,
    load_sources,
)
from db.enums import EventType, Priority, Region, SignalCategory
from parser.models import Publication
from parser.ru_stem import contains_keyword

# Порядок проверки типов событий: более специфичные сигналы (отмена, вступление в силу)
# проверяются раньше общего «изменение», чтобы не перекрывались.
_EVENT_TYPE_CHECK_ORDER = (
    EventType.REPEAL,
    EventType.ENTRY_INTO_FORCE,
    EventType.AMENDMENT,
    EventType.NEW_DOCUMENT,
)


@dataclasses.dataclass(frozen=True)
class ClassificationResult:
    is_relevant: bool
    categories: tuple[SignalCategory, ...]
    event_type: EventType
    region: Region
    priority: Priority


def _contains_any(text_lower: str, keywords: tuple[str, ...]) -> bool:
    return contains_keyword(text_lower, keywords)


def _publication_text(publication: Publication) -> str:
    return f"{publication.title} {publication.summary or ''}"


def match_categories(
    text: str, life_situations: tuple[LifeSituation, ...]
) -> tuple[SignalCategory, ...]:
    """AGENTS.md раздел 5.1 — публикация может относиться к нескольким ЖС одновременно
    (раздел 7: «Категория (одна или несколько ЖС)»)."""
    text_lower = text.lower()
    return tuple(
        situation.category for situation in life_situations if _contains_any(text_lower, situation.keywords)
    )


def detect_event_type(text: str, keywords: ClassificationKeywords) -> EventType:
    text_lower = text.lower()
    for event_type in _EVENT_TYPE_CHECK_ORDER:
        markers = keywords.event_type_markers.get(event_type, ())
        if _contains_any(text_lower, markers):
            return event_type
    return EventType.REVIEW  # раздел 4.3: без конкретного маркера — обзор


def detect_region(
    publication: Publication, regions: tuple[RegionEntry, ...], federal_domains: frozenset[str]
) -> Region:
    """AGENTS.md раздел 4.1: домен источника → «РФ» для федеральных → «Не определён».

    Текстовое уточнение (раздел 4.1: «регион может уточняться по тексту публикации») не
    реализовано: пока в справочнике всего 2 региона (Москва + РФ, AGENTS.md раздел 1),
    источник однозначно определяет регион без разбора текста — добавить при расширении
    за пределы MVP (AGENTS.md раздел 16, пункт 12).
    """
    source_domain = publication.source_key.split("/", 1)[0]
    for region_entry in regions:
        if any(source.domain == source_domain for source in region_entry.sources):
            return region_entry.region
    if source_domain in federal_domains:
        return Region.RF
    return Region.UNDEFINED


def detect_priority(text: str, keywords: ClassificationKeywords, region: Region) -> Priority:
    text_lower = text.lower()
    if not _contains_any(text_lower, keywords.document_markers):
        return Priority.LOW
    if region == Region.UNDEFINED:
        return Priority.MEDIUM
    if _contains_any(text_lower, keywords.priority_high_words):
        return Priority.HIGH
    return Priority.MEDIUM


@dataclasses.dataclass(frozen=True)
class Classifier:
    """Держит загруженные один раз справочники — не перечитывать YAML на каждую публикацию."""

    life_situations: tuple[LifeSituation, ...]
    keywords: ClassificationKeywords
    regions: tuple[RegionEntry, ...]
    federal_domains: frozenset[str]

    @classmethod
    def load(cls) -> Classifier:
        return cls(
            life_situations=load_life_situations(),
            keywords=load_classification_keywords(),
            regions=load_regions(),
            federal_domains=frozenset(source.domain for source in load_sources()["federal"]),
        )

    def classify(self, publication: Publication) -> ClassificationResult:
        text = _publication_text(publication)
        text_lower = text.lower()

        categories = match_categories(text, self.life_situations)
        is_relevant = bool(categories) and _contains_any(text_lower, self.keywords.topic_block)

        region = detect_region(publication, self.regions, self.federal_domains)
        event_type = detect_event_type(text, self.keywords)
        priority = detect_priority(text, self.keywords, region)

        return ClassificationResult(
            is_relevant=is_relevant,
            categories=categories,
            event_type=event_type,
            region=region,
            priority=priority,
        )
