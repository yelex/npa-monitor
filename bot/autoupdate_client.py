"""Адаптер к внешнему агенту автообновления (AGENTS.md раздел 3, раздел 16 п.8;
docs/SPEC_autoupdate_agent_contract.md).

Контракт реального агента (`/Users/user/dev/auto`, LangGraph-пайплайн `product_agent`)
не поднимается как HTTP-сервис — решение пользователя (спека, раздел 1): стык
минимальный, файловый spool-каталог (`AUTOUPDATE_SPOOL_DIR`). npa-monitor пишет задачу
в `tasks/sig-<signal_id>.json`, агент (за пределами этого репозитория) её забирает и
кладёт результат в `results/sig-<signal_id>.json`; `bot/main.py::scan_autoupdate_results`
читает результаты и архивирует их в `results/.processed/`. Транспорт переключается
конфигом (`AUTOUPDATE_MODE=spool|http`, спека раздел 3.1) — `bot/main.py` не завязан на
конкретную реализацию `send()`.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path
from typing import Any

from config import get_settings
from db.models import Signal

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2


class AutoUpdateModeNotSupported(NotImplementedError):
    """`AUTOUPDATE_MODE`, для которого нет реализации (спека, раздел 5 «НЕ входит»)."""


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """tmp-файл + `os.replace` — атомарно на одной ФС (спека, раздел 3.5)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def tasks_dir(spool_dir: Path) -> Path:
    return spool_dir / "tasks"


def results_dir(spool_dir: Path) -> Path:
    return spool_dir / "results"


def processed_dir(spool_dir: Path) -> Path:
    return results_dir(spool_dir) / ".processed"


def task_path(spool_dir: Path, signal_id: int) -> Path:
    return tasks_dir(spool_dir) / f"sig-{signal_id}.json"


def list_pending_results(spool_dir: Path) -> list[Path]:
    """`results/*.json`, не заходя в `results/.processed/` (спека, раздел 3.6)."""
    d = results_dir(spool_dir)
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.json") if p.is_file())


def archive_result(path: Path, spool_dir: Path) -> Path:
    """Перемещение обработанного результата в `results/.processed/` (спека, раздел 3.5)."""
    dest_dir = processed_dir(spool_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    os.replace(path, dest)
    return dest


class AutoUpdateAgentClient:
    """Спека, раздел 3.7. `send()` идемпотентен — повторный вызов с тем же
    `signal.id` перезаписывает `tasks/sig-<id>.json` (детерминированный `task_id`,
    раздел 3.3), это используется и стартовой сверкой (`bot/main.py::_reconcile_spool_tasks`)."""

    def send(
        self,
        signal: Signal,
        npa_url: str | None,
        discovery_url: str,
        categories: list[str],
        region: str,
        *,
        signal_type: str,
        measure_id: str | None,
        measure_row_hash: str | None,
        comment: str | None = None,
    ) -> Path:
        """docs/SPEC_signal_type_measure_select.md, раздел «Задача v2 и старые данные»:
        `signal_type`/`measure_id`/`measure_row_hash` — обязательные поля контракта v2
        коннектора npa-somas (спека v3). `comment` — необязательная пометка деградации
        (используется сверкой при перезаписи v1-задач без подтверждённого типа, см.
        `bot/main.py::_reconcile_spool_tasks`)."""
        settings = get_settings()
        if settings.autoupdate_mode != "spool":
            raise AutoUpdateModeNotSupported(
                f"AUTOUPDATE_MODE={settings.autoupdate_mode!r} не реализован (спека, раздел 5)"
            )
        spool_dir = Path(settings.autoupdate_spool_dir)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "task_id": f"sig-{signal.id}",
            "signal_id": signal.id,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "npa_url": npa_url,
            "discovery_url": discovery_url,
            "categories": categories,
            "region": region,
            "signal_type": signal_type,
            "measure_id": measure_id,
            "measure_row_hash": measure_row_hash,
        }
        if comment is not None:
            payload["comment"] = comment
        path = task_path(spool_dir, signal.id)
        _write_json_atomic(path, payload)
        log.info("автообновление: задача записана signal=%s -> %s", signal.id, path)
        return path
