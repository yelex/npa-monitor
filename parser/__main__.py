"""Точка входа для ежедневного обхода источников: `python -m parser`.

Планируется через cron/systemd timer (AGENTS.md раздел 14: 06:00) — см. README раздел
«Деплой». Не демон: один прогон по всем источникам и выход, как и `parser/orchestrator.
py::run_all` — падение одного источника не прерывает остальные, ошибка целиком
процесса возможна только на уровне самой БД (недоступна/повреждена).

`-v`/`--verbose` — включает подробный трейс по каждой публикации (какие ключевые
слова совпали, почему релевантно/нет, какой сигнал в итоге создан) — по запросу
пользователя, «видеть, как парсер принимает решение». См. README раздел «Запуск
парсера».

После обхода листингов — дополнительный проход через Yandex Search по каждой ЖС
(`parser/discovery_search.py::run_daily_discovery`, docs/SPEC_yandex_search_discovery.md,
раздел 5), если в `.env` заданы `YANDEX_SEARCH_API_KEY`/`YANDEX_SEARCH_FOLDER_ID`; если
нет — шаг пропускается с предупреждением в лог, не падает (эта подстраховка опциональна,
не обязательное условие готовности раздела 15 AGENTS.md).
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging

from config import get_settings
from db.session import init_db, make_engine, make_session_factory
from parser.classifier import Classifier
from parser.discovery_search import run_daily_discovery
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

    # config.get_settings() (не голый os.getenv) — иначе .env не читается сам по себе:
    # для этого нужен pydantic-settings с env_file=".env", как у bot/main.py. Баг найден
    # вживую: RU_PROXY_URL из .env не подхватывался, требовал ручного export в shell.
    settings = get_settings()
    db_path = settings.database_path
    ru_proxy_url = settings.ru_proxy_url or None

    engine = make_engine(db_path)
    init_db(engine)
    session_factory = make_session_factory(engine)
    classifier = Classifier.load()
    now = dt.datetime.now(dt.timezone.utc)

    with session_factory() as session:
        results = run_all(session, classifier=classifier, ru_proxy_url=ru_proxy_url, now=now)

        if settings.yandex_search_api_key and settings.yandex_search_folder_id:
            discovery_results = run_daily_discovery(
                session,
                classifier,
                api_key=settings.yandex_search_api_key,
                folder_id=settings.yandex_search_folder_id,
                now=now,
            )
        else:
            discovery_results = []
            log.info(
                "YANDEX_SEARCH_API_KEY/YANDEX_SEARCH_FOLDER_ID не заданы — "
                "поиск через Yandex Search пропущен (см. .env.example)"
            )

    ok = sum(1 for r in results if r.ok)
    new_signals = sum(r.new_signals for r in results)
    discovery_ok = sum(1 for r in discovery_results if r.ok)
    discovery_new_signals = sum(r.result.new_signals for r in discovery_results if r.result is not None)
    log.info(
        "готово: %d/%d источников ok, %d/%d ЖС в Yandex Search ok, всего новых сигналов: %d",
        ok,
        len(results),
        discovery_ok,
        len(discovery_results),
        new_signals + discovery_new_signals,
    )


if __name__ == "__main__":
    main()
