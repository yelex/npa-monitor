"""Одноразовая ретроактивная чистка уже накопленных сигналов на проде, PLAN.md Фаза 9
п.7, `docs/SPEC_retroactive_signals_cleanup.md`.

Применяет ту же логику, что фиксы Фазы 9 п.1/п.2/п.3/п.5 добавили в живой обход
источников (`parser/orchestrator.py`), к сигналам, уже созданным до этих фиксов.
Никогда не трогает сигналы вне статусов «Новый»/«Отложен» (раздел 3 спеки) и никогда не
отклоняет сигналы автоматически по релевантности (шаг D — только отчёт, раздел 5 спеки,
т.к. без сохранённого `summary` пересчёт релевантности по одному заголовку менее
надёжен, чем исходная классификация).

Запуск: `python -m scripts.cleanup_signals` (отчёт, без записи) или
`python -m scripts.cleanup_signals --apply` (шаги A/B/C применяются к БД, шаг D — всегда
только отчёт). Перед `--apply` на боевой БД — сделать бэкап файла (раздел 7 спеки).
"""
from __future__ import annotations

import argparse
import dataclasses
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from config import get_settings
from db.enums import RejectionReason, SignalStatus
from db.models import Signal
from db.service import transition_status
from db.session import init_db, make_engine, make_session_factory
from parser.classifier import Classifier, detect_priority
from parser.dedup import TITLE_DEDUP_WINDOW, canonicalize_url, find_duplicate_title
from parser.filters import is_excluded_path
from parser.llm import ClassifierLLMClient, get_default_client
from parser.models import Publication

log = logging.getLogger("cleanup_signals")

# Раздел 3 спеки: только статусы, где эксперт ещё не принял решение.
ACTIVE_STATUSES = (SignalStatus.NEW, SignalStatus.POSTPONED)


@dataclasses.dataclass
class RejectedItem:
    signal_id: int
    title: str | None
    reason: str


@dataclasses.dataclass
class PriorityChange:
    signal_id: int
    title: str | None
    old_priority: str
    new_priority: str


@dataclasses.dataclass
class RelevanceFlag:
    signal_id: int
    title: str | None
    trace: str


@dataclasses.dataclass
class CleanupReport:
    excluded_by_url: list[RejectedItem] = dataclasses.field(default_factory=list)
    duplicates_by_url: list[RejectedItem] = dataclasses.field(default_factory=list)
    duplicates_by_title: list[RejectedItem] = dataclasses.field(default_factory=list)
    priority_changes: list[PriorityChange] = dataclasses.field(default_factory=list)
    relevance_flags: list[RelevanceFlag] = dataclasses.field(default_factory=list)

    def format(self) -> str:
        lines = ["=== Ретроактивная чистка сигналов (PLAN.md Фаза 9 п.7) ==="]
        lines.append(f"Шаг A — исключены по URL-паттерну: {len(self.excluded_by_url)}")
        lines += [f"  [{i.signal_id}] {i.title!r} — {i.reason}" for i in self.excluded_by_url]
        lines.append(f"Шаг B1 — дубликаты по каноническому URL: {len(self.duplicates_by_url)}")
        lines += [f"  [{i.signal_id}] {i.title!r} — {i.reason}" for i in self.duplicates_by_url]
        lines.append(f"Шаг B2 — дубликаты по заголовку: {len(self.duplicates_by_title)}")
        lines += [f"  [{i.signal_id}] {i.title!r} — {i.reason}" for i in self.duplicates_by_title]
        lines.append(f"Шаг C — изменён приоритет: {len(self.priority_changes)}")
        lines += [
            f"  [{i.signal_id}] {i.title!r} — {i.old_priority} -> {i.new_priority}"
            for i in self.priority_changes
        ]
        lines.append(
            "Шаг D — кандидаты на ручную проверку релевантности "
            f"(не применяется автоматически): {len(self.relevance_flags)}"
        )
        lines += [f"  [{i.signal_id}] {i.title!r} — {i.trace}" for i in self.relevance_flags]
        return "\n".join(lines)


def _active_signals(session: Session) -> list[Signal]:
    stmt = select(Signal).where(Signal.status.in_(ACTIVE_STATUSES)).order_by(Signal.created_at, Signal.id)
    return list(session.scalars(stmt))


def _reject(session: Session, signal: Signal, *, rejection_reason: RejectionReason, reason: str) -> None:
    transition_status(
        session,
        signal,
        SignalStatus.REJECTED,
        changed_by="cleanup_script",
        reason=reason,
        rejection_reason=rejection_reason,
    )


def _step_a_url_exclusions(
    session: Session, signals: list[Signal], report: CleanupReport, *, apply: bool
) -> list[Signal]:
    remaining = []
    for signal in signals:
        if is_excluded_path(signal.source_url):
            reason = "Фаза 9 п.1/п.3: статичная справочная страница, ретроактивная чистка"
            report.excluded_by_url.append(RejectedItem(signal.id, signal.title, reason))
            if apply:
                _reject(session, signal, rejection_reason=RejectionReason.NOT_NPA, reason=reason)
        else:
            remaining.append(signal)
    return remaining


def _step_b1_url_dedup(
    session: Session, signals: list[Signal], report: CleanupReport, *, apply: bool
) -> list[Signal]:
    groups: dict[str, list[Signal]] = {}
    for signal in signals:
        groups.setdefault(canonicalize_url(signal.source_url), []).append(signal)

    remaining = []
    for group in groups.values():
        if len(group) == 1:
            remaining.append(group[0])
            continue
        group.sort(key=lambda s: (s.created_at, s.id))
        keeper, *dupes = group
        remaining.append(keeper)
        for dupe in dupes:
            reason = (
                f"Фаза 9 п.2: дубликат по каноническому URL сигнала [{keeper.id}], "
                "ретроактивная чистка"
            )
            report.duplicates_by_url.append(RejectedItem(dupe.id, dupe.title, reason))
            if apply:
                _reject(session, dupe, rejection_reason=RejectionReason.DUPLICATE, reason=reason)
    remaining.sort(key=lambda s: (s.created_at, s.id))
    return remaining


def _step_b2_title_dedup(
    session: Session,
    signals: list[Signal],
    report: CleanupReport,
    *,
    apply: bool,
    llm_client: ClassifierLLMClient | None,
) -> list[Signal]:
    kept: list[Signal] = []
    for signal in signals:
        if not signal.title:
            kept.append(signal)
            continue
        candidates = [
            (s.id, s.title, s.id)
            for s in kept
            if s.title and signal.created_at - s.created_at <= TITLE_DEDUP_WINDOW
        ]
        match = find_duplicate_title(signal.title, candidates, llm_client=llm_client)
        if match is None:
            kept.append(signal)
            continue
        _, keeper_id = match
        reason = (
            f"Фаза 9 п.2: дубликат по содержанию заголовка сигнала [{keeper_id}], "
            "ретроактивная чистка"
        )
        report.duplicates_by_title.append(RejectedItem(signal.id, signal.title, reason))
        if apply:
            _reject(session, signal, rejection_reason=RejectionReason.DUPLICATE, reason=reason)
    return kept


def _step_c_priority(
    classifier: Classifier, signals: list[Signal], report: CleanupReport, *, apply: bool
) -> None:
    for signal in signals:
        new_priority = detect_priority(signal.title or "", classifier.keywords, signal.region)
        if new_priority != signal.priority:
            report.priority_changes.append(
                PriorityChange(signal.id, signal.title, signal.priority.value, new_priority.value)
            )
            if apply:
                signal.priority = new_priority


def _step_d_relevance_audit(classifier: Classifier, signals: list[Signal], report: CleanupReport) -> None:
    for signal in signals:
        pub = Publication(
            source_key="cleanup_script",
            title=signal.title or "",
            url=signal.source_url,
            published_at=signal.created_at,
        )
        trace = classifier.explain(pub)
        if not trace.result.is_relevant:
            report.relevance_flags.append(RelevanceFlag(signal.id, signal.title, trace.format()))


def run_cleanup(
    session: Session,
    classifier: Classifier,
    *,
    apply: bool = False,
    llm_client: ClassifierLLMClient | None = None,
) -> CleanupReport:
    """Раздел 4 `docs/SPEC_retroactive_signals_cleanup.md`. Шаги строго по порядку —
    каждый следующий видит только сигналы, оставшиеся активными после предыдущего. Шаг D
    никогда не пишет в БД, независимо от `apply` (раздел 5 спеки)."""
    report = CleanupReport()

    signals = _active_signals(session)
    signals = _step_a_url_exclusions(session, signals, report, apply=apply)
    signals = _step_b1_url_dedup(session, signals, report, apply=apply)
    signals = _step_b2_title_dedup(session, signals, report, apply=apply, llm_client=llm_client)
    _step_c_priority(classifier, signals, report, apply=apply)
    _step_d_relevance_audit(classifier, signals, report)

    if apply:
        session.commit()

    return report


def main() -> None:
    cli_parser = argparse.ArgumentParser(description=__doc__)
    cli_parser.add_argument(
        "--apply",
        action="store_true",
        help="применить шаги A/B/C к БД (по умолчанию — только отчёт, БД не меняется)",
    )
    args = cli_parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    settings = get_settings()
    engine = make_engine(settings.database_path)
    init_db(engine)
    session_factory = make_session_factory(engine)
    classifier = Classifier.load()

    with session_factory() as session:
        report = run_cleanup(session, classifier, apply=args.apply, llm_client=get_default_client())

    print(report.format())
    if not args.apply:
        print("\n(режим отчёта — БД не изменена; для применения шагов A/B/C передайте --apply)")


if __name__ == "__main__":
    main()
