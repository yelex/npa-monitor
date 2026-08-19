"""Сборка сигнала из результата классификации, AGENTS.md раздел 6/7, PLAN.md Фаза 4."""
from __future__ import annotations

from sqlalchemy.orm import Session

from db.models import Signal
from db.service import create_signal
from parser.classifier import ClassificationResult
from parser.models import Publication


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
