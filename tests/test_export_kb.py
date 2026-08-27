"""scripts/export_kb.py, docs/SPEC_result_edit.md §3.4: экспорт KB + overlay,
пересчёт row_hash только у затронутых записей, атомарная запись."""
from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session

from db.models import MeasureOverride
from db.session import init_db, make_engine, make_session_factory
from scripts.export_kb import export_kb


@pytest.fixture
def session(tmp_path) -> Session:
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    factory = make_session_factory(engine)
    with factory() as session:
        yield session


@pytest.fixture
def kb_path(tmp_path):
    rows = [
        {
            "measure_id": "00_svo_1", "measure_name": "Выплата контрактнику",
            "measure_sum": "195 000 ₽", "row_hash": "hash-svo-1",
        },
        {
            "measure_id": "00_vbd_1", "measure_name": "Выплата ветерану",
            "measure_sum": "10 000 ₽", "row_hash": "hash-vbd-1",
        },
    ]
    path = tmp_path / "kb.json"
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return path


def test_export_kb_applies_latest_override_and_recomputes_row_hash(session: Session, kb_path) -> None:
    session.add(MeasureOverride(
        measure_id="00_svo_1", field="measure_sum", old_value="195 000 ₽", new_value="200 000 ₽",
        source="agent_diff", task_id="sig-1",
    ))
    session.commit()

    touched = export_kb(session, kb_path=kb_path)

    assert touched == 1
    rows = json.loads(kb_path.read_text(encoding="utf-8"))
    svo = next(r for r in rows if r["measure_id"] == "00_svo_1")
    vbd = next(r for r in rows if r["measure_id"] == "00_vbd_1")
    assert svo["measure_sum"] == "200 000 ₽"
    assert svo["row_hash"] != "hash-svo-1"  # пересчитан, содержимое изменилось
    assert vbd["row_hash"] == "hash-vbd-1"  # не затронут overlay'ем — хэш не трогаем
    assert vbd["measure_sum"] == "10 000 ₽"


def test_export_kb_applies_latest_of_multiple_overrides_on_same_field(session: Session, kb_path) -> None:
    import datetime as dt

    session.add(MeasureOverride(
        measure_id="00_svo_1", field="measure_sum", new_value="200 000 ₽",
        source="agent_diff", task_id="sig-1",
        changed_at=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
    ))
    session.add(MeasureOverride(
        measure_id="00_svo_1", field="measure_sum", new_value="210 000 ₽",
        source="agent_diff", task_id="sig-2",
        changed_at=dt.datetime(2026, 8, 20, tzinfo=dt.timezone.utc),
    ))
    session.commit()

    export_kb(session, kb_path=kb_path)

    rows = json.loads(kb_path.read_text(encoding="utf-8"))
    svo = next(r for r in rows if r["measure_id"] == "00_svo_1")
    assert svo["measure_sum"] == "210 000 ₽"


def test_export_kb_deleted_field_becomes_null(session: Session, kb_path) -> None:
    session.add(MeasureOverride(
        measure_id="00_vbd_1", field="measure_sum", old_value="10 000 ₽", new_value=None,
        source="agent_diff", task_id="sig-1",
    ))
    session.commit()

    export_kb(session, kb_path=kb_path)

    rows = json.loads(kb_path.read_text(encoding="utf-8"))
    vbd = next(r for r in rows if r["measure_id"] == "00_vbd_1")
    assert vbd["measure_sum"] is None
    assert "measure_sum" in vbd  # ключ остаётся — схема KB фиксированная (спека)


def test_export_kb_without_overrides_leaves_file_content_unchanged(session: Session, kb_path) -> None:
    before = kb_path.read_text(encoding="utf-8")
    touched = export_kb(session, kb_path=kb_path)
    after = kb_path.read_text(encoding="utf-8")

    assert touched == 0
    assert json.loads(before) == json.loads(after)


def test_export_kb_writes_atomically_no_leftover_tmp_file(session: Session, kb_path) -> None:
    session.add(MeasureOverride(
        measure_id="00_svo_1", field="measure_sum", new_value="200 000 ₽",
        source="agent_diff", task_id="sig-1",
    ))
    session.commit()

    export_kb(session, kb_path=kb_path)

    assert not kb_path.with_name(kb_path.name + ".tmp").exists()
