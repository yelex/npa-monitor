"""Аналитическая сверка (только чтение, НИКОГДА не пишет в БД), PLAN.md Фаза 9 п.7,
docs/SPEC_retroactive_signals_cleanup.md, раздел 8.

`scripts/cleanup_signals.py` на боевой БД нашёл 0 активных сигналов («Новый»/
«Отложен») — эксперт к моменту запуска уже вручную разобрал весь дамп (91 из 105
сигналов в статусе «Отклонён»). Вопрос: сколько из этих 91 ручных отклонений поймала
бы та же логика (URL-исключения п.1/п.3, дедуп по URL/заголовку п.2, аудит
релевантности п.6), если бы она уже действовала на момент их создания — как сверка
«фиксы Фазы 9 действительно закрывают то, что эксперт чистил руками», а не только как
абстрактное утверждение из PLAN.md.

Не изменяет ни одного сигнала — не вызывает `init_db()` (боевая БД уже
инициализирована, лишняя идемпотентная ALTER-миграция схемы здесь не нужна) и не
принимает `--apply`, в отличие от `scripts/cleanup_signals.py`.

Запуск: `./scripts/audit_rejected_signals.sh` (по аналогии с dump_signals.sh/
cleanup_signals.sh — самодостаточный файл, скармливается в контейнер через stdin, не
зависит от того, задеплоен ли сам этот файл на сервере).
"""
from __future__ import annotations

import argparse
import collections
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from config import get_settings
from db.enums import SignalStatus
from db.models import Signal
from db.session import make_engine, make_session_factory
from parser.classifier import Classifier
from parser.dedup import TITLE_DEDUP_WINDOW, canonicalize_url, find_duplicate_title
from parser.filters import is_excluded_path
from parser.llm import ClassifierLLMClient, get_default_client
from parser.models import Publication

log = logging.getLogger("audit_rejected_signals")


def _rejected_signals(session: Session) -> list[Signal]:
    stmt = (
        select(Signal).where(Signal.status == SignalStatus.REJECTED).order_by(Signal.created_at, Signal.id)
    )
    return list(session.scalars(stmt))


def audit(
    signals: list[Signal], classifier: Classifier, *, llm_client: ClassifierLLMClient | None = None
) -> dict[int, str]:
    """Для каждого `signal.id` из `signals` — строка с тем, каким шагом (A/B1/B2/D)
    его поймала бы автоматика Фазы 9, только если поймала хотя бы одним. Шаг C
    (приоритет) сюда не входит — он не про причину отклонения."""
    caught: dict[int, str] = {}

    remaining = []
    for signal in signals:
        if is_excluded_path(signal.source_url):
            caught[signal.id] = "A: URL-паттерн (п.1/п.3)"
        else:
            remaining.append(signal)

    kept_by_url: dict[str, Signal] = {}
    still_remaining = []
    for signal in remaining:
        key = canonicalize_url(signal.source_url)
        if key in kept_by_url:
            caught[signal.id] = f"B1: дубль URL сигнала [{kept_by_url[key].id}] (п.2)"
        else:
            kept_by_url[key] = signal
            still_remaining.append(signal)

    kept_titles: list[Signal] = []
    remaining2 = []
    for signal in still_remaining:
        if not signal.title:
            remaining2.append(signal)
            continue
        candidates = [
            (s.id, s.title, s.id)
            for s in kept_titles
            if s.title and signal.created_at - s.created_at <= TITLE_DEDUP_WINDOW
        ]
        match = find_duplicate_title(signal.title, candidates, llm_client=llm_client)
        if match is not None:
            _, keeper_id = match
            caught[signal.id] = f"B2: дубль заголовка сигнала [{keeper_id}] (п.2)"
        else:
            kept_titles.append(signal)
            remaining2.append(signal)

    for signal in remaining2:
        pub = Publication(
            source_key="audit_script",
            title=signal.title or "",
            url=signal.source_url,
            published_at=signal.created_at,
        )
        trace = classifier.explain(pub)
        if not trace.result.is_relevant:
            caught[signal.id] = "D: нерелевантно по текущему классификатору (п.6)"

    return caught


def format_report(signals: list[Signal], caught: dict[int, str]) -> str:
    lines = ["=== Сверка: причины отклонения эксперта vs автоматика Фазы 9 (только чтение) ==="]
    lines.append(f"Всего отклонённых сигналов: {len(signals)}")
    lines.append(f"Поймано хотя бы одним автоматическим признаком: {len(caught)}")

    by_step = collections.Counter(value.split(":")[0] for value in caught.values())
    for step in ("A", "B1", "B2", "D"):
        lines.append(f"  шаг {step}: {by_step.get(step, 0)}")

    lines.append("")
    lines.append("Причины ручного отклонения эксперта (rejection_reason), все отклонённые:")
    reason_counts = collections.Counter(
        (s.rejection_reason.value if s.rejection_reason else "—") for s in signals
    )
    for reason, count in reason_counts.most_common():
        lines.append(f"  {reason}: {count}")

    not_caught = [s for s in signals if s.id not in caught]
    lines.append("")
    lines.append(f"НЕ пойманные автоматикой ({len(not_caught)}) — причины ручного отклонения:")
    nc_counts = collections.Counter(
        (s.rejection_reason.value if s.rejection_reason else "—") for s in not_caught
    )
    for reason, count in nc_counts.most_common():
        lines.append(f"  {reason}: {count}")

    lines.append("")
    lines.append("Подробности по пойманным автоматикой:")
    for signal in signals:
        if signal.id not in caught:
            continue
        expert_reason = signal.rejection_reason.value if signal.rejection_reason else "—"
        comment = f" ({signal.rejection_comment})" if signal.rejection_comment else ""
        lines.append(
            f"  [{signal.id}] {signal.title!r} — автоматика: {caught[signal.id]}; "
            f"эксперт: {expert_reason}{comment}"
        )
    return "\n".join(lines)


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    logging.basicConfig(level=logging.INFO)

    settings = get_settings()
    engine = make_engine(settings.database_path)
    session_factory = make_session_factory(engine)
    classifier = Classifier.load()

    with session_factory() as session:
        signals = _rejected_signals(session)
        caught = audit(signals, classifier, llm_client=get_default_client())

    print(format_report(signals, caught))


if __name__ == "__main__":
    main()
