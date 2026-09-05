"""Алерт о деградации источников парсера, docs/SPEC_source_health_alert.md.

Инцидент 02.09: RU-прокси лежал ~4 дня, `parser/__main__.py` писал только WARNING в лог
cron-контейнера — эксперт заметил деградацию только «на глаз» (мало новостей), никакого
сигнала в Telegram не было. Парсер на каждую неудачную попытку обхода источника пишет
`SourceState.last_attempt_at`/`consecutive_failures` (`db.service.record_source_failure`,
вызывается из `parser/orchestrator.py`) — этот модуль читает их и решает, нужно ли перед
очередной утренней сводкой (`bot/main.py::_digest_loop`) прислать эксперту ⚠️-алерт.

Два независимых триггера (спека, раздел «Предполагаемый фикс»): `check_source_health`
(источник с >=2 неудачными попытками подряд) и `check_zero_signal_degradation` (весь
прогон не нашёл ни одного нового сигнала, и ни один ЖС-значимый источник не обходился
успешно) — второй ловит деградацию, которую первый может пропустить (напр. источники
формально «успешны», просто ничего не находят, или сбой не фиксируется как неудача, как
у `parser/discovery_search.py::run_daily_discovery`).

Модуль не знает про Telegram/aiogram — только БД и state-файл дедупа; отправку делает
вызывающая сторона (`bot/main.py`).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Signal, SourceState
from parser.discovery_search import SOURCE_KEY as YANDEX_SEARCH_SOURCE_KEY
from parser.sources import government, kremlin, pravo_gov

log = logging.getLogger(__name__)

# spec: «≥2 подряд неудачных попыток за 24ч»
MIN_CONSECUTIVE_FAILURES = 2
# Источник, по которому давно не было ни одной попытки (снят из data/sources.yaml и т.п.),
# не должен вечно висеть в алертах — считаем только попытки за последние сутки.
ATTEMPT_FRESHNESS_WINDOW = dt.timedelta(hours=24)
# Дедуп: не слать один и тот же алерт по источнику на каждой утренней сводке подряд.
ALERT_DEDUP_TTL = dt.timedelta(hours=24)

DEFAULT_STATE_PATH = Path("data/health_alerts_state.json")

# Спека, раздел «Предполагаемый фикс», второй пункт: источники, без которых парсер в
# принципе не может найти ЖС-значимую публикацию (federal-листинги + Yandex Search
# per-ЖС-запись `yandex_search:<situation_id>`, docs/SPEC_yandex_search_discovery.md) —
# используется вторым триггером ниже (не путать с «иными» источниками из раздела 4
# AGENTS.md, они контекстные, не основной канал обнаружения).
YZS_SIGNIFICANT_SOURCE_PREFIXES = (
    pravo_gov.SOURCE_KEY,
    kremlin.SOURCE_KEY,
    government.SOURCE_KEY,
    f"{YANDEX_SEARCH_SOURCE_KEY}:",
)

# Ключ дедупа второго триггера в том же state-файле, что и per-источник алерты — с
# префиксом "__", чтобы cleanup в sources_due_for_alert (ниже) его не затирал: тот
# cleanup стирает всё, чего нет в текущем списке больных source_key.
ZERO_SIGNAL_RUN_ALERT_KEY = "__zero_signal_run__"


@dataclasses.dataclass(frozen=True)
class UnhealthySource:
    source_key: str
    consecutive_failures: int
    last_success_at: dt.datetime | None
    last_attempt_at: dt.datetime


def check_source_health(session: Session, *, now: dt.datetime | None = None) -> list[UnhealthySource]:
    """Источники, у которых `last_attempt_at` значительно свежее `last_success_at` —
    минимум `MIN_CONSECUTIVE_FAILURES` неудачных попыток подряд, последняя из них не
    старше `ATTEMPT_FRESHNESS_WINDOW`. Отсортировано по `source_key` для стабильного
    порядка в сообщении."""
    now = now or dt.datetime.now(dt.timezone.utc)
    states = session.scalars(select(SourceState)).all()

    unhealthy = [
        UnhealthySource(
            source_key=state.source_key,
            consecutive_failures=state.consecutive_failures,
            last_success_at=state.last_success_at,
            last_attempt_at=state.last_attempt_at,
        )
        for state in states
        if state.consecutive_failures >= MIN_CONSECUTIVE_FAILURES
        and state.last_attempt_at is not None
        and now - state.last_attempt_at <= ATTEMPT_FRESHNESS_WINDOW
    ]
    unhealthy.sort(key=lambda u: u.source_key)
    return unhealthy


def format_alert(sources: list[UnhealthySource]) -> str:
    """Текст ⚠️-алерта, отправляется первым сообщением перед утренней сводкой."""
    lines = ["⚠️ Деградация источников парсера:"]
    for u in sources:
        since = (
            u.last_success_at.strftime("%d.%m.%Y %H:%M")
            if u.last_success_at is not None
            else "начала наблюдений"
        )
        lines.append(f"• {u.source_key}: недоступен с {since} ({u.consecutive_failures} попыток подряд)")
    lines.append("Проверьте прокси/ключи (RU_PROXY_URL, YANDEX_SEARCH_*).")
    return "\n".join(lines)


def _load_state(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.exception("health_alerts: не удалось прочитать state-файл %s, считаем пустым", path)
        return {}


def _save_state(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # атомарная замена — не оставить state-файл битым при сбое посреди записи


def sources_due_for_alert(
    sources: list[UnhealthySource],
    *,
    state_path: Path = DEFAULT_STATE_PATH,
    now: dt.datetime | None = None,
) -> list[UnhealthySource]:
    """Дедуп алерта (спека, раздел «Реализация» п.3): источник, по которому алерт уже
    отправлялся в пределах `ALERT_DEDUP_TTL`, не попадает в результат повторно. Источники,
    прошедшие фильтр, считаются отправленными — их отметка в state-файле обновляется как
    побочный эффект (вызывающая сторона должна реально отправить сообщение сразу после
    вызова, до следующего вызова этой функции)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    state = _load_state(state_path)

    due = []
    for u in sources:
        last_alert_raw = state.get(u.source_key)
        if last_alert_raw is not None and now - dt.datetime.fromisoformat(last_alert_raw) < ALERT_DEDUP_TTL:
            continue
        due.append(u)

    if not due:
        return due

    for u in due:
        state[u.source_key] = now.isoformat()
    # источники, переставшие быть больными, не должны держать место в state-файле вечно;
    # "__"-ключи (напр. ZERO_SIGNAL_RUN_ALERT_KEY) принадлежат другому триггеру — не трогаем.
    still_unhealthy = {u.source_key for u in sources}
    state = {
        key: value for key, value in state.items() if key in still_unhealthy or key.startswith("__")
    }
    _save_state(state_path, state)
    return due


def _zero_signal_window_start(now_local: dt.datetime) -> dt.datetime:
    """Начало окна проверки второго триггера — момент самого недавнего планового
    запуска парсера (cron `0 6 * * 1-5`, 06:00 локального времени, см. SPEC_weekend_
    zero_signal_window.md): в сб/вс и пн-до-прогона окно накрывает пятничный прогон,
    и успешный пятничный обход не считается деградацией. Если расписание сменится —
    менять здесь вместе с crontab."""
    candidate = now_local.replace(hour=6, minute=0, second=0, microsecond=0)
    # 5=суббота, 6=воскресенье: последний плановый день — пятница (откат на 2 дня);
    # понедельник до 06:00 — тоже пятница. Остальные дни до 06:00 — предыдущий день.
    if candidate > now_local:
        candidate -= dt.timedelta(days=1)
    while candidate.weekday() >= 5:  # сб/вс не входят в расписание 1-5
        candidate -= dt.timedelta(days=1)
    return candidate


def check_zero_signal_degradation(session: Session, *, now: dt.datetime | None = None) -> bool:
    """Второй триггер спеки (раздел «Предполагаемый фикс», п.2): со времени последнего
    планового прогона парсера не появилось ни одного нового сигнала И ни один
    ЖС-значимый источник (`YZS_SIGNIFICANT_SOURCE_PREFIXES`) не обходился успешно
    за то же окно. Окно — не фиксированные 24 часа, а от последнего планового запуска
    (06:00 пн-пт по cron): фиксированные сутки ложно срабатывали в выходные, когда
    пятничный прогон выпадал из окна (инцидент 05.09, SPEC_weekend_zero_signal_window.md).

    Отличие от `check_source_health`: тот ловит деградацию отдельных источников по
    счётчику неудачных попыток; этот — более грубый, но более надёжный сигнал «парсер в
    целом перестал находить публикации» даже если ни один источник не набрал
    `MIN_CONSECUTIVE_FAILURES` неудач подряд (например, все попытки формально успешны —
    источник просто ничего не отдаёт — или неудачи фиксируются не для всех типов
    источников, см. `parser/discovery_search.py::run_daily_discovery`, не вызывает
    `mark_source_failed`). Без условия «0 новых сигналов» алерт ложно сработал бы в
    спокойный день, когда федеральные источники обошлись успешно, но публикаций не было.

    Отдельная защита от ложного срабатывания на ещё ни разу не запускавшемся парсере
    (`sources_state` целиком пустая — не с чем сравнивать, деградации не бывает без
    предыдущей истории обходов): в этом случае функция молчит."""
    has_ever_run = session.scalar(select(SourceState.source_key).limit(1)) is not None
    if not has_ever_run:
        return False

    now = now or dt.datetime.now(dt.timezone.utc)
    # БД хранит наивные UTC-таймстемпы; окно тоже считаем в UTC. Расписание cron (06:00)
    # привязано к локальному времени хоста — Europe/Amsterdam (сервер гейтвея),
    # хост-инфраструктура npa-monitor живёт в том же поясе.
    now_local = now.astimezone(tz=dt.timezone(dt.timedelta(hours=2)))  # Amsterdam CEST
    window_start_local = _zero_signal_window_start(now_local)
    since = window_start_local.astimezone(dt.timezone.utc)

    has_recent_signal = session.scalar(select(Signal.id).where(Signal.created_at >= since).limit(1))
    if has_recent_signal is not None:
        return False

    states = session.scalars(select(SourceState)).all()
    significant = [s for s in states if s.source_key.startswith(YZS_SIGNIFICANT_SOURCE_PREFIXES)]
    any_recent_success = any(
        s.last_success_at is not None and s.last_success_at >= since for s in significant
    )
    return not any_recent_success


def format_zero_signal_alert() -> str:
    """Текст ⚠️-алерта для второго триггера — источники не перечисляются (проблема не в
    одном источнике, а в отсутствии успешных обходов вообще), но подсказка та же."""
    names = ", ".join(prefix.rstrip(":") for prefix in YZS_SIGNIFICANT_SOURCE_PREFIXES)
    return (
        "⚠️ За последние сутки не найдено ни одного нового сигнала, и ни один из "
        f"ключевых источников ({names}) не обходился успешно за это же время.\n"
        "Проверьте прокси/ключи (RU_PROXY_URL, YANDEX_SEARCH_*)."
    )


def zero_signal_alert_due(*, state_path: Path = DEFAULT_STATE_PATH, now: dt.datetime | None = None) -> bool:
    """Дедуп второго триггера — тот же TTL/state-файл, что и `sources_due_for_alert`, но
    отдельный "__"-ключ (не source_key), чтобы не путать области двух триггеров."""
    now = now or dt.datetime.now(dt.timezone.utc)
    state = _load_state(state_path)

    last_alert_raw = state.get(ZERO_SIGNAL_RUN_ALERT_KEY)
    if last_alert_raw is not None and now - dt.datetime.fromisoformat(last_alert_raw) < ALERT_DEDUP_TTL:
        return False

    state[ZERO_SIGNAL_RUN_ALERT_KEY] = now.isoformat()
    _save_state(state_path, state)
    return True
