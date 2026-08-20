"""Точка входа для ежедневного обхода источников: `python -m parser`.

Планируется через cron/systemd timer (AGENTS.md раздел 14: 06:00) — см. README раздел
«Деплой». Не демон: один прогон по всем источникам и выход, как и `parser/orchestrator.
py::run_all` — падение одного источника не прерывает остальные, ошибка целиком
процесса возможна только на уровне самой БД (недоступна/повреждена).

`-v`/`--verbose` — включает подробный трейс по каждой публикации (какие ключевые
слова совпали, почему релевантно/нет, какой сигнал в итоге создан) — по запросу
пользователя, «видеть, как парсер принимает решение». См. README раздел «Запуск
парсера».
"""
from __future__ import annotations

import argparse
import logging
import os

from db.session import init_db, make_engine, make_session_factory
from parser.orchestrator import run_all

log = logging.getLogger("parser")


def main() -> None:
    cli_parser = argparse.ArgumentParser(description=__doc__)
    cli_parser.add_argument(
        "-v", "--verbose", action="store_true", help="подробный трейс классификации по каждой публикации"
    )
    args = cli_parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    if args.verbose:
        # DEBUG только для нашего кода — иначе httpx/httpcore на DEBUG заваливают
        # вывод построчными логами TCP/TLS-хендшейка, а не тем, что нужно ("видеть,
        # как парсер принимает решение").
        logging.getLogger("parser").setLevel(logging.DEBUG)

    db_path = os.getenv("DATABASE_PATH", "data/npa_monitor.db")
    ru_proxy_url = os.getenv("RU_PROXY_URL") or None

    engine = make_engine(db_path)
    init_db(engine)
    session_factory = make_session_factory(engine)

    with session_factory() as session:
        results = run_all(session, ru_proxy_url=ru_proxy_url)

    ok = sum(1 for r in results if r.ok)
    new_signals = sum(r.new_signals for r in results)
    log.info("готово: %d/%d источников ok, всего новых сигналов: %d", ok, len(results), new_signals)


if __name__ == "__main__":
    main()
