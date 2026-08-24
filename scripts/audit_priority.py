"""Сверка regex- и LLM-приоритета с решениями эксперта (только чтение), PLAN.md Фаза 11
п.4, `docs/SPEC_llm_priority.md`, раздел «Решения» п.6 («Аудит»).

По образцу `scripts/audit_rejected_signals.py`: не пишет в БД, не принимает `--apply`.
Для каждого сигнала со стored-приоритетом MEDIUM/LOW прогоняет
`parser/llm_priority.py::refine_priorities_batch` (тот же путь, что использовал бы
`parser/orchestrator.py::run_all` в бою) и сравнивает получившийся LLM-скорректированный
приоритет с фактическим исходом эксперта: статус сигнала (REJECTED/SENT_TO_AGENT/
COMPLETED/…) и время `NEW -> IN_PROGRESS` из `status_history` (гипотеза спеки: эксперт
быстрее берёт в работу сигналы с более высоким приоритетом — если LLM-приоритет
коррелирует с этим лучше regex, это аргумент за `LLM_PRIORITY_APPLY=1`).

Запуск: `python -m scripts.audit_priority`.
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from config import get_settings
from db.enums import Priority, SignalStatus
from db.models import Signal
from db.session import make_engine, make_session_factory
from parser.llm import ClassifierLLMClient, get_default_client
from parser.llm_priority import chunk_signal_ids, refine_priorities_batch

log = logging.getLogger("audit_priority")

_PRIORITY_REPORT_ORDER = (Priority.HIGH, Priority.MEDIUM, Priority.LOW)


@dataclasses.dataclass
class SignalAudit:
    signal_id: int
    title: str | None
    status: SignalStatus
    regex_priority: Priority
    llm_priority: Priority | None  # None — LLM не сконфигурирован/не изменил бы приоритет
    final_priority: Priority
    time_to_in_progress: dt.timedelta | None


def _time_to_in_progress(signal: Signal) -> dt.timedelta | None:
    for entry in signal.history:
        if entry.to_status == SignalStatus.IN_PROGRESS:
            return entry.changed_at - signal.created_at
    return None


def audit(session: Session, *, llm_client: ClassifierLLMClient | None) -> list[SignalAudit]:
    """Для каждого сигнала — regex-, LLM- и итоговый приоритет (комбинирование —
    `parser.llm_priority._combine`, максимум один уровень сдвига) + время взятия в
    работу. HIGH-сигналы в LLM не прогоняются (тот же скоуп, что и в бою) — для них
    regex_priority == llm_priority == final_priority == сохранённый `Signal.priority`.
    """
    signals = list(session.scalars(select(Signal).order_by(Signal.created_at, Signal.id)))
    eligible_ids = [s.id for s in signals if s.priority != Priority.HIGH]

    refinements_by_id = {}
    for chunk in chunk_signal_ids(eligible_ids):
        for refinement in refine_priorities_batch(session, chunk, llm_client):
            refinements_by_id[refinement.signal_id] = refinement

    audits = []
    for signal in signals:
        refinement = refinements_by_id.get(signal.id)
        audits.append(
            SignalAudit(
                signal_id=signal.id,
                title=signal.title,
                status=signal.status,
                regex_priority=refinement.regex_priority if refinement else signal.priority,
                llm_priority=refinement.llm_priority if refinement else None,
                final_priority=refinement.final_priority if refinement else signal.priority,
                time_to_in_progress=_time_to_in_progress(signal),
            )
        )
    return audits


def format_report(audits: list[SignalAudit]) -> str:
    lines = ["=== Сверка regex vs LLM приоритета (PLAN.md Фаза 11 п.4, только чтение) ==="]
    lines.append(f"Всего сигналов: {len(audits)}")

    changed = [a for a in audits if a.llm_priority is not None and a.final_priority != a.regex_priority]
    lines.append(f"LLM сдвинул бы приоритет на один уровень: {len(changed)}")
    lines += [
        f"  [{a.signal_id}] {a.title!r} — {a.regex_priority.value} -> {a.final_priority.value}"
        for a in changed
    ]

    discrepancies = [
        a
        for a in audits
        if a.llm_priority is not None
        and a.final_priority == a.regex_priority
        and a.llm_priority != a.regex_priority
    ]
    lines.append(f"Расхождения >1 уровня (low<->high, не применяются): {len(discrepancies)}")
    lines += [
        f"  [{a.signal_id}] {a.title!r} — regex={a.regex_priority.value} llm={a.llm_priority.value}"
        for a in discrepancies
    ]

    lines.append("")
    lines.append("По статусам (кол-во по regex-приоритету / по итоговому LLM-приоритету):")
    for status in SignalStatus:
        subset = [a for a in audits if a.status == status]
        if not subset:
            continue
        regex_counts = collections.Counter(a.regex_priority.value for a in subset)
        final_counts = collections.Counter(a.final_priority.value for a in subset)
        lines.append(
            f"  {status.value} ({len(subset)}): regex={dict(regex_counts)} final={dict(final_counts)}"
        )

    lines.append("")
    lines.append("Среднее время NEW -> IN_PROGRESS по итоговому приоритету (гипотеза спеки):")
    for priority in _PRIORITY_REPORT_ORDER:
        deltas = [
            a.time_to_in_progress
            for a in audits
            if a.final_priority == priority and a.time_to_in_progress is not None
        ]
        if deltas:
            avg = sum(deltas, dt.timedelta()) / len(deltas)
            lines.append(f"  {priority.value}: среднее {avg} (сигналов: {len(deltas)})")
        else:
            lines.append(f"  {priority.value}: нет данных (никто ещё не взят в работу)")

    return "\n".join(lines)


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    logging.basicConfig(level=logging.INFO)

    settings = get_settings()
    engine = make_engine(settings.database_path)
    session_factory = make_session_factory(engine)

    with session_factory() as session:
        audits = audit(session, llm_client=get_default_client())

    print(format_report(audits))


if __name__ == "__main__":
    main()
