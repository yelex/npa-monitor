"""Write-back результатов агента автообновления в базу мер, docs/SPEC_result_edit.md.

KB (`data/benefits_knowledge_base.json`) — перезаливаемый снапшот (см.
`db/measures.py`): правки аналитика хранятся отдельно, в `measure_overrides`
(overlay), и применяются к файлу только через `scripts/export_kb.py`, не патчат
его напрямую (§3.3).
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.measures import kb_field_names, load_raw_record
from db.models import MeasureOverride, Signal, SignalResult

log = logging.getLogger("db.overrides")

# Действия чекбоксов карточки диффа, которые реально пишут значение при «Применить»
# (§3.4): accept — принять `now` из диффа, delete — new_value=null, custom — своё
# значение (введено через ✏, reply). "skip" сюда не входит — поле просто не трогается.
_SELECTABLE_ACTIONS = frozenset({"accept", "delete", "custom"})


def upsert_signal_result(
    session: Session, *, task_id: str, signal_id: int | None, payload: dict[str, Any]
) -> SignalResult:
    """Ревью №3: парсенный payload результата сохраняется в БД до архивации
    spool-файла (`bot/main.py::scan_autoupdate_results`) — идемпотентно по
    `task_id` (повторный скан того же результата перезаписывает, не дублирует)."""
    existing = session.scalar(select(SignalResult).where(SignalResult.task_id == task_id))
    if existing is not None:
        existing.payload = payload
        existing.signal_id = signal_id
        session.flush()
        return existing
    result = SignalResult(task_id=task_id, signal_id=signal_id, payload=payload)
    session.add(result)
    session.flush()
    return result


def get_signal_result(session: Session, task_id: str) -> SignalResult | None:
    return session.scalar(select(SignalResult).where(SignalResult.task_id == task_id))


def latest_override_value(session: Session, *, measure_id: str, field: str) -> tuple[bool, Any]:
    """Последний (по `changed_at`, затем `id`) override на `(measure_id, field)`."""
    stmt = (
        select(MeasureOverride)
        .where(MeasureOverride.measure_id == measure_id, MeasureOverride.field == field)
        .order_by(MeasureOverride.changed_at.desc(), MeasureOverride.id.desc())
        .limit(1)
    )
    ov = session.scalar(stmt)
    return (True, ov.new_value) if ov is not None else (False, None)


def effective_value(
    session: Session, *, kb_row: dict[str, Any] | None, measure_id: str, field: str
) -> Any:
    """KB + overlay (§3.3): последний override на поле, иначе — сырое значение из
    снапшота KB."""
    has_override, value = latest_override_value(session, measure_id=measure_id, field=field)
    if has_override:
        return value
    return kb_row.get(field) if kb_row is not None else None


def overridden_measure_ids(session: Session, measure_ids: list[str]) -> dict[str, dt.date]:
    """Дата последней правки на меру — бейдж «✏️ правлено» в саджесте
    (`bot/main.py::measure_label`)."""
    if not measure_ids:
        return {}
    stmt = select(MeasureOverride.measure_id, MeasureOverride.changed_at).where(
        MeasureOverride.measure_id.in_(measure_ids)
    )
    latest: dict[str, dt.datetime] = {}
    for measure_id, changed_at in session.execute(stmt):
        if measure_id not in latest or changed_at > latest[measure_id]:
            latest[measure_id] = changed_at
    return {measure_id: ts.date() for measure_id, ts in latest.items()}


@dataclass(frozen=True)
class ApplyConflict:
    field: str
    idx: int
    expected_was: Any
    actual_value: Any


@dataclass
class ApplyResult:
    applied_fields: list[str] = field(default_factory=list)
    skipped_fields: list[str] = field(default_factory=list)
    rejected_unwhitelisted: list[str] = field(default_factory=list)
    conflicts: list[ApplyConflict] = field(default_factory=list)
    stale: bool = False


class NoMeasureForTask(ValueError):
    """У сигнала нет привязанной меры (`new`-задача либо мера не выбрана) —
    write-back недоступен (спека §3.1, MVP)."""


def apply_selection(
    session: Session,
    *,
    signal_result: SignalResult,
    changed_by: int | str | None,
    kb_path: str | Path,
) -> ApplyResult:
    """Применяет отмеченные аналитиком поля `signal_result.selection` к overlay
    (§3.4). Одна транзакция на батч (ревью №10): либо все проходящие проверку поля
    добавляются одним коммитом, либо — при конфликте (ревью №9, `ApplyResult.
    conflicts` непустой) — ни одно. Сама не коммитит и не открывает транзакцию —
    вызывающий код (`bot/main.py`) коммитит после проверки `result.conflicts`.
    """
    payload = signal_result.payload or {}
    changes: list[dict[str, Any]] = payload.get("changes") or []
    selection: dict[str, Any] = signal_result.selection or {}

    signal = session.get(Signal, signal_result.signal_id) if signal_result.signal_id else None
    measure_id = signal.measure_id if signal else None
    base_row_hash = signal.measure_row_hash if signal else None
    if not measure_id:
        raise NoMeasureForTask(
            f"задача {signal_result.task_id}: сигнал без привязанной меры, write-back недоступен"
        )

    kb_row = load_raw_record(measure_id, path=kb_path)
    whitelist = kb_field_names(kb_path)

    to_apply: list[tuple[dict[str, Any], Any]] = []
    result = ApplyResult()

    for idx, change in enumerate(changes):
        sel = selection.get(str(idx)) or {}
        action = sel.get("action")
        if action not in _SELECTABLE_ACTIONS:
            result.skipped_fields.append(change.get("field", str(idx)))
            continue
        field_name = change.get("field")
        if field_name not in whitelist:
            result.rejected_unwhitelisted.append(field_name)
            log.warning(
                "overrides: поле %r вне схемы KB, task=%s — override отклонён",
                field_name, signal_result.task_id,
            )
            continue
        new_value = None if action == "delete" else (
            sel.get("value") if action == "custom" else change.get("now")
        )
        current = effective_value(session, kb_row=kb_row, measure_id=measure_id, field=field_name)
        expected_was = change.get("was")
        if current != expected_was:
            # Ревью №9: поле уже менялось после этого диффа — не пишем молча.
            result.conflicts.append(
                ApplyConflict(field=field_name, idx=idx, expected_was=expected_was, actual_value=current)
            )
            continue
        to_apply.append((change, new_value))

    if result.conflicts:
        return result

    for change, new_value in to_apply:
        field_name = change["field"]
        existing = session.scalar(
            select(MeasureOverride).where(
                MeasureOverride.measure_id == measure_id,
                MeasureOverride.field == field_name,
                MeasureOverride.task_id == signal_result.task_id,
            )
        )
        if existing is not None:
            continue  # §3.3 дедуп: тот же результат уже применён к этому полю
        session.add(
            MeasureOverride(
                measure_id=measure_id,
                field=field_name,
                old_value=change.get("was"),
                new_value=new_value,
                signal_id=signal_result.signal_id,
                source="agent_diff",
                changed_by=str(changed_by) if changed_by is not None else None,
                task_id=signal_result.task_id,
                base_row_hash=base_row_hash,
            )
        )
        result.applied_fields.append(field_name)

    if result.applied_fields and kb_row is not None and base_row_hash is not None:
        # Иная запись, чем та, от которой считался дифф (менялась чем-то ещё
        # помимо только что применённых полей) — не блокирует запись (полевой
        # конфликт уже проверен выше), но стоит показать аналитику на пересмотр.
        result.stale = kb_row.get("row_hash") != base_row_hash

    session.flush()
    return result
