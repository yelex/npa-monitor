"""Engine/сессии SQLite (WAL), PLAN.md раздел 2. Без Alembic в MVP."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base

DEFAULT_DB_PATH = Path("npa_monitor.db")

# (таблица, колонка, DDL-тип) — точечный обход отсутствия Alembic (раздел выше) для
# колонок, добавленных в уже развёрнутую боевую БД: create_all не меняет существующие
# таблицы, а на VPS уже накоплены строки documents_seen (пилот с 2026-08-20). Не замена
# полноценных миграций — только для этого случая, см. docs/SPEC_content_dedup.md, раздел 3.1.
_COLUMNS_ADDED_AFTER_INITIAL_SCHEMA = (
    ("documents_seen", "title", "TEXT"),
    # docs/SPEC_signal_type_measure_select.md: тип сигнала + row_hash выбранной меры.
    ("signals", "signal_type", "VARCHAR(16)"),
    ("signals", "measure_row_hash", "VARCHAR(64)"),
    # docs/SPEC_source_health_alert.md: последняя попытка обхода + счётчик неудач подряд.
    ("sources_state", "last_attempt_at", "DATETIME"),
    ("sources_state", "consecutive_failures", "INTEGER NOT NULL DEFAULT 0"),
)


def make_engine(db_path: str | Path = DEFAULT_DB_PATH) -> Engine:
    """SQLite engine с WAL-режимом (один эксперт, десятки сигналов/день — PLAN.md раздел 2)."""
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    """create_all — вся DDL-схема, миграции (Alembic) появятся на следующем этапе.

    Перед create_all — точечная подстраховка для колонок, добавленных к уже
    существующим таблицам после первого деплоя (`_COLUMNS_ADDED_AFTER_INITIAL_SCHEMA`):
    create_all создаёт только отсутствующие таблицы целиком, не добавляет колонки в уже
    существующие.
    """
    _ensure_columns(engine)
    Base.metadata.create_all(engine)


def _ensure_columns(engine: Engine) -> None:
    with engine.begin() as conn:
        for table, column, ddl_type in _COLUMNS_ADDED_AFTER_INITIAL_SCHEMA:
            existing_tables = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if existing_tables is None:
                continue  # таблицы ещё нет — её создаст create_all с полной схемой ниже
            columns = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if column not in columns:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
