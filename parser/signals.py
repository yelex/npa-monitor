"""Сборка сигнала из результата классификации, AGENTS.md раздел 6/7, PLAN.md Фаза 4."""
from __future__ import annotations

from sqlalchemy.orm import Session

from db.enums import EventType
from db.models import Signal
from db.service import create_signal
from parser.classifier import ClassificationResult
from parser.models import Publication


def is_review(classification: ClassificationResult) -> bool:
    """docs/SPEC_no_reviews_no_stale_reminders.md, п.1 / docs/SPEC_review_filter_discovery.md:
    обзоры/агрегаторы (нет маркера события 5.4, `detect_event_type` вернул REVIEW) не
    содержат конкретики по отдельной новости — сигнал для них не создаётся. Общая точка
    для оркестратора (`parser/orchestrator.py`) и Yandex-discovery
    (`parser/discovery_search.py`), чтобы фильтр не расходился по путям создания сигнала.
    """
    return classification.is_relevant and classification.event_type == EventType.REVIEW


def build_signal(
    session: Session, publication: Publication, classification: ClassificationResult
) -> Signal | None:
    """Создаёт сигнал в БД (статус «Новый», см. `db.service.create_signal`), если
    публикация релевантна; иначе возвращает `None` — сигнал просто не создаётся
    (AGENTS.md раздел 6: сигнал создаётся для каждой релевантной публикации), а не
    переводится в «Отклонён» (это ручное действие эксперта, не парсера).

    Дедупликация (`db.service.register_document_seen`) — забота вызывающего кода
    (оркестратор, PLAN.md Фаза 6), не этой функции: публикации, которые уже видели, не
    должны доходить до классификации вовсе.
    """
    if not classification.is_relevant:
        return None

    return create_signal(
        session,
        event_type=classification.event_type,
        priority=classification.priority,
        source_url=publication.url,
        categories=list(classification.categories),
        region=classification.region,
        title=publication.title,
    )
