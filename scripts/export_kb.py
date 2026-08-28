"""scripts/export_kb.py — экспорт KB + overlay (`measure_overrides`) в новый
снапшот `benefits_knowledge_base.json`, docs/SPEC_result_edit.md §3.4.

Вызывается ботом синхронно после каждого успешного «Применить»
(`bot/main.py::on_override_apply` -> `db/overrides.py::apply_selection`) — ревью
№1 (блокер, не опция): без немедленного экспорта следующий прогон агента по той
же мере посчитает дифф от до-overlay baseline и наврёт в match-метках.

Пересчитывает `row_hash` только у записей, которые overlay реально затронул —
не оригинальный алгоритм заливки npa-somas (неизвестен, в этом репозитории не
встречается нигде, кроме готовых значений в самом снапшоте), но этого не
требуется: единственное, для чего `row_hash` используется на стороне бота —
STALE-детект (сравнение с `Signal.measure_row_hash`, зафиксированным на момент
отправки задачи агенту), а он самосогласован, пока хэш детерминирован и
меняется при изменении содержимого записи.

`export_kb` сбрасывает кэш сырых KB-строк (`db/measures.py::invalidate_kb_cache`,
docs/SPEC_fix_review_75af72b.md) сразу после записи файла — `load_raw_record`/
`kb_field_names` (write-back overlay, STALE-детект) видят новый снапшот без
рестарта бота. Индекс поиска мер (`db/measures.py::_load_records`,
`MeasureRecord`) отдельным `lru_cache` не затронут этим сбросом — новые
названия/теги в поиске появятся только после рестарта; это осознанное
упрощение MVP (SPEC_result_edit.md, раздел «Решения по открытым вопросам» п.3).

Запуск вручную: `python -m scripts.export_kb` (тот же путь, что в `.env`
`BENEFITS_KNOWLEDGE_BASE_PATH`, по умолчанию `data/benefits_knowledge_base.json`).
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from db.measures import invalidate_kb_cache
from db.models import MeasureOverride

log = logging.getLogger("export_kb")


def _row_hash(row: dict) -> str:
    payload = {k: v for k, v in row.items() if k != "row_hash"}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.md5(canonical.encode("utf-8")).hexdigest()


def _latest_overrides_by_measure(session: Session) -> dict[str, dict[str, MeasureOverride]]:
    """Последний (по `changed_at`, затем `id`) override на каждое `(measure_id,
    field)` — эффективное состояние overlay для экспорта."""
    by_measure: dict[str, dict[str, MeasureOverride]] = {}
    overrides = session.query(MeasureOverride).order_by(
        MeasureOverride.changed_at, MeasureOverride.id
    )
    for ov in overrides:
        by_measure.setdefault(ov.measure_id, {})[ov.field] = ov
    return by_measure


def export_kb(session: Session, *, kb_path: str | Path) -> int:
    """Перезаписывает `kb_path` KB-снапшотом с применённым overlay (атомарно,
    tmp-файл + `replace`). Возвращает число записей, затронутых overlay'ем."""
    path = Path(kb_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    overrides_by_measure = _latest_overrides_by_measure(session)

    touched = 0
    for row in raw:
        measure_id = row.get("measure_id")
        if not measure_id:
            continue
        fields = overrides_by_measure.get(measure_id)
        if not fields:
            continue
        for field_name, override in fields.items():
            row[field_name] = override.new_value
        row["row_hash"] = _row_hash(row)
        touched += 1

    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)
    invalidate_kb_cache()

    log.warning(
        "export_kb: %s перезаписан (%d записей затронуто overlay); кэш сырых "
        "KB-строк сброшен (db/measures.py::invalidate_kb_cache), поисковый "
        "индекс мер по-прежнему требует перезапуска бота (MVP)",
        path, touched,
    )
    return touched


def main() -> None:
    from config import get_settings
    from db.session import init_db, make_engine, make_session_factory

    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    engine = make_engine(settings.database_path)
    init_db(engine)
    session_factory = make_session_factory(engine)

    with session_factory() as session:
        touched = export_kb(session, kb_path=settings.benefits_knowledge_base_path)

    print(f"export_kb: затронуто overlay записей: {touched}")


if __name__ == "__main__":
    main()
