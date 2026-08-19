"""Engine/сессии SQLite (WAL), PLAN.md раздел 2. Без Alembic в MVP."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from db.models import Base

DEFAULT_DB_PATH = Path("npa_monitor.db")


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
    """create_all — вся DDL-схема, миграции (Alembic) появятся на следующем этапе."""
    Base.metadata.create_all(engine)


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
