"""db/overrides.py, docs/SPEC_result_edit.md: overlay measure_overrides поверх KB,
whitelist полей, проверка конфликта, атомарность батча, дедуп."""
from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from db.enums import REGION_RF, EventType, Priority, SignalCategory
from db.models import MeasureOverride, Signal, SignalResult
from db.overrides import (
    ApplyConflict,
    NoMeasureForTask,
    apply_selection,
    effective_value,
    get_signal_result,
    latest_override_value,
    overridden_measure_ids,
    upsert_signal_result,
)
from db.service import create_signal
from db.session import init_db, make_engine, make_session_factory


@pytest.fixture
def session(tmp_path) -> Session:
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        yield session


@pytest.fixture
def kb_path(tmp_path) -> str:
    rows = [
        {
            "measure_id": "00_svo_1",
            "measure_name": "Выплата при заключении контракта",
            "measure_sum": "195 000 ₽",
            "region": "РФ",
            "row_hash": "hash-v1",
        }
    ]
    path = tmp_path / "kb.json"
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _make_signal_with_measure(session: Session, *, measure_id: str, measure_row_hash: str | None) -> Signal:
    signal = create_signal(
        session,
        event_type=EventType.AMENDMENT,
        priority=Priority.HIGH,
        source_url="https://mintrud.gov.ru/docs/1",
        categories=[SignalCategory.SVO],
        region=REGION_RF,
        title="Тестовый сигнал",
    )
    signal.measure_id = measure_id
    signal.measure_row_hash = measure_row_hash
    session.commit()
    return signal


def _make_signal_result(
    session: Session, *, signal: Signal | None, task_id: str, changes: list[dict],
    selection: dict | None = None,
) -> SignalResult:
    sr = upsert_signal_result(
        session,
        task_id=task_id,
        signal_id=signal.id if signal else None,
        payload={"schema_version": 3, "task_id": task_id, "status": "done", "changes": changes},
    )
    if selection is not None:
        sr.selection = selection
    session.commit()
    return sr


# --- upsert_signal_result / get_signal_result ---------------------------------


def test_upsert_signal_result_creates_then_updates_by_task_id(session: Session) -> None:
    upsert_signal_result(session, task_id="sig-1", signal_id=None, payload={"status": "done"})
    session.commit()
    assert session.query(SignalResult).count() == 1

    upsert_signal_result(session, task_id="sig-1", signal_id=None, payload={"status": "error"})
    session.commit()

    assert session.query(SignalResult).count() == 1
    assert get_signal_result(session, "sig-1").payload == {"status": "error"}


def test_get_signal_result_returns_none_for_unknown_task_id(session: Session) -> None:
    assert get_signal_result(session, "sig-missing") is None


# --- effective_value / latest_override_value -----------------------------------


def test_effective_value_falls_back_to_kb_row_without_override(session: Session) -> None:
    kb_row = {"measure_sum": "195 000 ₽"}
    assert effective_value(session, kb_row=kb_row, measure_id="00_svo_1", field="measure_sum") == "195 000 ₽"


def test_effective_value_prefers_latest_override(session: Session) -> None:
    session.add(MeasureOverride(
        measure_id="00_svo_1", field="measure_sum", old_value="195 000 ₽", new_value="200 000 ₽",
        source="agent_diff", task_id="sig-1",
    ))
    session.commit()
    kb_row = {"measure_sum": "195 000 ₽"}
    assert effective_value(session, kb_row=kb_row, measure_id="00_svo_1", field="measure_sum") == "200 000 ₽"


def test_latest_override_value_picks_most_recent_by_changed_at(session: Session) -> None:
    import datetime as dt

    old = MeasureOverride(
        measure_id="00_svo_1", field="measure_sum", new_value="200 000 ₽",
        source="agent_diff", task_id="sig-1",
        changed_at=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
    )
    new = MeasureOverride(
        measure_id="00_svo_1", field="measure_sum", new_value="210 000 ₽",
        source="agent_diff", task_id="sig-2",
        changed_at=dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc),
    )
    session.add_all([old, new])
    session.commit()

    has_override, value = latest_override_value(session, measure_id="00_svo_1", field="measure_sum")
    assert has_override is True
    assert value == "210 000 ₽"


# --- apply_selection ------------------------------------------------------------


def test_apply_selection_raises_when_signal_has_no_measure_id(session: Session, kb_path: str) -> None:
    signal = _make_signal_with_measure(session, measure_id=None, measure_row_hash=None)
    sr = _make_signal_result(
        session, signal=signal, task_id="sig-1",
        changes=[{"field": "measure_sum", "was": "195 000 ₽", "now": "200 000 ₽", "match": "exact"}],
        selection={"0": {"action": "accept"}},
    )
    with pytest.raises(NoMeasureForTask):
        apply_selection(session, signal_result=sr, changed_by=111, kb_path=kb_path)


def test_apply_selection_applies_accepted_field(session: Session, kb_path: str) -> None:
    signal = _make_signal_with_measure(session, measure_id="00_svo_1", measure_row_hash="hash-v1")
    sr = _make_signal_result(
        session, signal=signal, task_id="sig-1",
        changes=[{"field": "measure_sum", "was": "195 000 ₽", "now": "200 000 ₽", "match": "exact"}],
        selection={"0": {"action": "accept"}},
    )
    result = apply_selection(session, signal_result=sr, changed_by=111, kb_path=kb_path)
    session.commit()

    assert result.applied_fields == ["measure_sum"]
    assert not result.conflicts
    override = session.query(MeasureOverride).filter_by(measure_id="00_svo_1", field="measure_sum").one()
    assert override.new_value == "200 000 ₽"
    assert override.old_value == "195 000 ₽"
    assert override.base_row_hash == "hash-v1"
    assert override.changed_by == "111"


def test_apply_selection_delete_sets_new_value_null(session: Session, kb_path: str) -> None:
    signal = _make_signal_with_measure(session, measure_id="00_svo_1", measure_row_hash="hash-v1")
    sr = _make_signal_result(
        session, signal=signal, task_id="sig-1",
        changes=[{"field": "measure_sum", "was": "195 000 ₽", "now": "200 000 ₽", "match": "exact"}],
        selection={"0": {"action": "delete"}},
    )
    result = apply_selection(session, signal_result=sr, changed_by=111, kb_path=kb_path)
    session.commit()

    override = session.query(MeasureOverride).filter_by(measure_id="00_svo_1", field="measure_sum").one()
    assert override.new_value is None
    assert result.applied_fields == ["measure_sum"]


def test_apply_selection_custom_value_uses_selection_value_not_diff_now(
    session: Session, kb_path: str
) -> None:
    signal = _make_signal_with_measure(session, measure_id="00_svo_1", measure_row_hash="hash-v1")
    sr = _make_signal_result(
        session, signal=signal, task_id="sig-1",
        changes=[{"field": "measure_sum", "was": "195 000 ₽", "now": "200 000 ₽", "match": "exact"}],
        selection={"0": {"action": "custom", "value": "205 000 ₽"}},
    )
    result = apply_selection(session, signal_result=sr, changed_by=111, kb_path=kb_path)
    session.commit()

    override = session.query(MeasureOverride).filter_by(measure_id="00_svo_1", field="measure_sum").one()
    assert override.new_value == "205 000 ₽"
    assert result.applied_fields == ["measure_sum"]


def test_apply_selection_skips_unselected_fields(session: Session, kb_path: str) -> None:
    signal = _make_signal_with_measure(session, measure_id="00_svo_1", measure_row_hash="hash-v1")
    sr = _make_signal_result(
        session, signal=signal, task_id="sig-1",
        changes=[{"field": "measure_sum", "was": "195 000 ₽", "now": "200 000 ₽", "match": "exact"}],
        selection={},
    )
    result = apply_selection(session, signal_result=sr, changed_by=111, kb_path=kb_path)

    assert result.applied_fields == []
    assert result.skipped_fields == ["measure_sum"]
    assert session.query(MeasureOverride).count() == 0


def test_apply_selection_rejects_field_outside_kb_whitelist(session: Session, kb_path: str) -> None:
    signal = _make_signal_with_measure(session, measure_id="00_svo_1", measure_row_hash="hash-v1")
    sr = _make_signal_result(
        session, signal=signal, task_id="sig-1",
        changes=[{"field": "not_a_real_kb_field", "was": "x", "now": "y", "match": "exact"}],
        selection={"0": {"action": "accept"}},
    )
    result = apply_selection(session, signal_result=sr, changed_by=111, kb_path=kb_path)

    assert result.applied_fields == []
    assert result.rejected_unwhitelisted == ["not_a_real_kb_field"]
    assert session.query(MeasureOverride).count() == 0


def test_apply_selection_conflict_blocks_entire_batch(session: Session, kb_path: str) -> None:
    """Ревью №9: `was` из диффа не совпадает с текущим эффективным значением (кто-то
    уже применил override к этому полю) — конфликт блокирует весь батч (ревью №10,
    атомарность), даже если другое поле в батче конфликта не имеет."""
    signal = _make_signal_with_measure(session, measure_id="00_svo_1", measure_row_hash="hash-v1")
    # Поле уже поменяно другим overlay после диффа.
    session.add(MeasureOverride(
        measure_id="00_svo_1", field="measure_sum", old_value="195 000 ₽", new_value="199 000 ₽",
        source="manual", task_id="sig-0",
    ))
    session.commit()

    sr = _make_signal_result(
        session, signal=signal, task_id="sig-1",
        changes=[
            {"field": "measure_sum", "was": "195 000 ₽", "now": "200 000 ₽", "match": "exact"},
            {
                "field": "measure_name", "was": "Выплата при заключении контракта",
                "now": "Новое имя", "match": "exact",
            },
        ],
        selection={"0": {"action": "accept"}, "1": {"action": "accept"}},
    )
    result = apply_selection(session, signal_result=sr, changed_by=111, kb_path=kb_path)

    assert len(result.conflicts) == 1
    assert result.conflicts[0] == ApplyConflict(
        field="measure_sum", idx=0, expected_was="195 000 ₽", actual_value="199 000 ₽"
    )
    assert result.applied_fields == []
    # Только override, добавленный до вызова — новых строк батч не создал.
    assert session.query(MeasureOverride).count() == 1


def test_apply_selection_dedup_same_task_id_field_is_noop(session: Session, kb_path: str) -> None:
    """§3.3 дедуп: повторное «Применить» по тому же результату не плодит вторую
    строку и не третий раз применять то же самое поле того же task_id."""
    signal = _make_signal_with_measure(session, measure_id="00_svo_1", measure_row_hash="hash-v1")
    sr = _make_signal_result(
        session, signal=signal, task_id="sig-1",
        changes=[{"field": "measure_sum", "was": "195 000 ₽", "now": "200 000 ₽", "match": "exact"}],
        selection={"0": {"action": "accept"}},
    )
    first = apply_selection(session, signal_result=sr, changed_by=111, kb_path=kb_path)
    session.commit()
    assert first.applied_fields == ["measure_sum"]

    # Повторный вызов (например, даблтап «Применить») — эффективное значение теперь
    # равно override.new_value, а не kb_row["measure_sum"], значит "was" уже не
    # совпадает -> конфликт, а не тихий дубль. Это ожидаемо: карточка после первого
    # успешного применения убирает клавиатуру (bot/main.py), повторный вызов
    # смоделирован здесь напрямую по низкоуровневому apply_selection.
    second = apply_selection(session, signal_result=sr, changed_by=111, kb_path=kb_path)
    assert second.conflicts
    assert session.query(MeasureOverride).count() == 1


def test_apply_selection_flags_stale_when_kb_row_hash_changed_since_diff(
    session: Session, kb_path: str
) -> None:
    signal = _make_signal_with_measure(session, measure_id="00_svo_1", measure_row_hash="stale-hash")
    sr = _make_signal_result(
        session, signal=signal, task_id="sig-1",
        changes=[{"field": "measure_sum", "was": "195 000 ₽", "now": "200 000 ₽", "match": "exact"}],
        selection={"0": {"action": "accept"}},
    )
    result = apply_selection(session, signal_result=sr, changed_by=111, kb_path=kb_path)

    assert result.applied_fields == ["measure_sum"]
    assert result.stale is True  # kb_path row_hash="hash-v1" != signal.measure_row_hash="stale-hash"


# --- overridden_measure_ids ------------------------------------------------------


def test_overridden_measure_ids_returns_latest_date_per_measure(session: Session) -> None:
    import datetime as dt

    session.add(MeasureOverride(
        measure_id="00_svo_1", field="measure_sum", new_value="200 000 ₽",
        source="agent_diff", task_id="sig-1",
        changed_at=dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc),
    ))
    session.add(MeasureOverride(
        measure_id="00_svo_1", field="department", new_value="Минобороны",
        source="agent_diff", task_id="sig-2",
        changed_at=dt.datetime(2026, 8, 25, tzinfo=dt.timezone.utc),
    ))
    session.commit()

    result = overridden_measure_ids(session, ["00_svo_1", "00_vbd_1"])
    assert result == {"00_svo_1": dt.date(2026, 8, 25)}


def test_overridden_measure_ids_empty_list_returns_empty_dict(session: Session) -> None:
    assert overridden_measure_ids(session, []) == {}
