"""Классификация публикаций и формирование сигнала, PLAN.md Фаза 4,
AGENTS.md разделы 5.1, 6.1, 7.

Правила:
- Релевантность = совпадение по ЖС-ключам И (тематический блок мер ИЛИ маркер
  документа/сути) — по разделу 5.1 «прямое указание на ЖС + маркер».
- Приоритет: высокий — прямое указание на ЖС + маркер изменения + есть ссылка;
  средний — косвенная связь / нет текста акта / регион не определён;
  низкий — аналитика/обзор без конкретного документа.
- Регион: домен источника → уточнение по тексту → «РФ» для федеральных,
  «Не определён» при неудаче (со средним приоритетом).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import re

from db.enums import EventType, Priority, Region, SignalCategory
from parser.models import Publication

# --- Словари ключевых слов (AGENTS.md раздел 5.1/6.1) -----------------------

MEASURE_KEYWORDS = (
    "мера поддержки",
    "выплата",
    "льгота",
    "пособие",
    "компенсация",
    "субсидия",
)

DOCUMENT_MARKER_RE = re.compile(
    r"\b(?:постановление|указ|закон|приказ|распоряжение)\b", re.IGNORECASE
)
ACTION_MARKER_RE = re.compile(
    r"\b(?:принят|утвержд[её]н|подписан|вступ(?:ает|ил)\s+в\s+силу|внесены\s+изменения)\b",
    re.IGNORECASE,
)

REVIEW_MARKER_RE = re.compile(
    r"\b(?:обзор|интервью|аналитик[аи]|комментарий)\b", re.IGNORECASE
)

REPEAL_RE = re.compile(r"\b(?:утратил\w*\s+силу|отмен[её]n?н?\b|признан\s+утратившим)", re.IGNORECASE)
AMENDMENT_RE = re.compile(r"\b(?:внесени\w+\s+изменени\w+|изменени\w*\s+в\s+\w[а-я]*\s*(?:закон|постановлен|указ|приказ))", re.IGNORECASE)
ENTRY_INTO_FORCE_RE = re.compile(
    r"\bвступ(?:ает|ил)\s+в\s+силу\s+(?:с\s+)?(\d{1,2}[./]\d{1,2}[./]\d{2,4})", re.IGNORECASE
)
NEW_DOCUMENT_RE = re.compile(r"\b(?:принят|утвержд[её]н|подписан)\b", re.IGNORECASE)

# Регион: Москва-маркеры в тексте (региональные источники уже дают регион по домену).
MOSCOW_RE = re.compile(r"\b(?:города\s+Москвы|г\.?\s*Москв[аеы]|Правительств\w+\s+Москвы|мэра\s+Москвы|УМ\b)", re.IGNORECASE)

_FEDERAL_DOMAINS = (
    "publication.pravo.gov.ru",
    "kremlin.ru",
    "government.ru",
    "sfr.gov.ru",
    "mintrud.gov.ru",
)

# --- Результат классификации ------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Classification:
    """Результат классификации одной публикации."""

    relevant: bool
    categories: tuple[SignalCategory, ...]
    event_type: EventType | None
    priority: Priority | None
    region: Region | None
    effective_from: dt.date | None  # дата вступления в силу (regex-эвристика)
    reason: str  # почему релевантно/нет — для отладки и логов


def _match_groups(text: str, groups: dict[SignalCategory, tuple[str, ...]]) -> tuple[SignalCategory, ...]:
    lowered = text.lower()
    return tuple(cat for cat, kws in groups.items() if any(kw.lower() in lowered for kw in kws))


def _parse_date(raw: str) -> dt.date | None:
    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%d.%m.%y"):
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def classify(pub: Publication, ls_keywords: dict[SignalCategory, tuple[str, ...]]) -> Classification:
    """Классифицировать публикацию. `ls_keywords` — справочник ЖС из db/catalog.py."""

    text = f"{pub.title} {pub.summary or ''}".strip()
    lowered = text.lower()

    categories = _match_groups(text, ls_keywords)
    has_measure = any(kw.lower() in lowered for kw in MEASURE_KEYWORDS)
    has_doc_marker = bool(DOCUMENT_MARKER_RE.search(text))
    has_action_marker = bool(ACTION_MARKER_RE.search(text))

    # Регион: домен → текст → федеральный → не определён.
    from parser.filters import domain_of  # локальный импорт: избегаем цикла на уровне модуля

    host = domain_of(pub.url)
    if host.endswith("mos.ru") or host.endswith("dszn.ru"):
        region = Region.MOSCOW
    elif any(host == d or host.endswith(f".{d}") for d in _FEDERAL_DOMAINS):
        region = Region.RF
    elif MOSCOW_RE.search(text):
        region = Region.MOSCOW
    else:
        region = Region.UNDEFINED

    # Тип события (порядок: отмена > изменение > вступление в силу > новый > обзор).
    if REPEAL_RE.search(text):
        event_type = EventType.REPEAL
    elif AMENDMENT_RE.search(text):
        event_type = EventType.AMENDMENT
    elif ENTRY_INTO_FORCE_RE.search(text):
        event_type = EventType.ENTRY_INTO_FORCE
    elif REVIEW_MARKER_RE.search(text) and not (has_doc_marker and has_action_marker):
        event_type = EventType.REVIEW
    elif has_doc_marker or has_action_marker:
        event_type = EventType.NEW_DOCUMENT
    else:
        event_type = None

    m = ENTRY_INTO_FORCE_RE.search(text)
    effective_from = _parse_date(m.group(1)) if m else None

    # --- Релевантность: ЖС-совпадение + (меры или маркер документа/сути) ----
    if not categories:
        return Classification(False, (), None, None, None, None, "нет совпадений по ЖС")

    if not (has_measure or has_doc_marker or has_action_marker):
        return Classification(False, (), None, None, None, None, "ЖС есть, но нет мер/маркеров документа")

    # --- Приоритет ----------------------------------------------------------
    is_review = event_type is EventType.REVIEW
    if is_review:
        priority = Priority.LOW
    elif categories and has_action_marker and pub.url:
        priority = Priority.HIGH
    elif region is Region.UNDEFINED or pub.summary is None:
        priority = Priority.MEDIUM
    else:
        priority = Priority.MEDIUM

    reason = (
        f"ЖС: {', '.join(c.value for c in categories)}; "
        f"меры={has_measure}, док={has_doc_marker}, суть={has_action_marker}"
    )
    return Classification(True, categories, event_type, priority, region, effective_from, reason)


def _format_date(value: dt.date | None) -> str | None:
    return value.isoformat() if value else None


def classification_to_signal_kwargs(cls_: Classification, pub: Publication) -> dict | None:
    """Собрать kwargs для db.service.create_signal из классификации.

    Возвращает None для нерелевантных публикаций. Категории ЖС записываются
    вызывающим кодом через связь M2M (create_signal принимает их отдельно).
    """
    if not cls_.relevant:
        return None
    return {
        "event_type": cls_.event_type,
        "priority": cls_.priority,
        "title": pub.title[:500],
        "region": cls_.region,
        "source_url": pub.url,
        "rejection_reason": None,
        "comment": cls_.reason[:500],
        # published_at/effective_from вызывающий код передаёт отдельно,
        # см. db.service.create_signal.
    }
