"""Тесты parser/__main__.py, PLAN.md Фаза 8.

Регрессия, найденная вживую при проверке деплоя: `main()` читал DATABASE_PATH/
RU_PROXY_URL через голый `os.getenv`, который не подхватывает `.env` (в отличие от
`config.get_settings()`, которым пользуется bot/main.py) — RU_PROXY_URL из `.env`
молча игнорировался, все ru_proxy-источники падали с ValueError.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

import parser.__main__ as parser_main


def test_main_reads_ru_proxy_url_from_env_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("RU_PROXY_URL", "http://proxy.example:8888")
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "")
    parser_main.get_settings.cache_clear()
    monkeypatch.setattr(sys, "argv", ["parser"])

    run_all = MagicMock(return_value=[])
    monkeypatch.setattr(parser_main, "run_all", run_all)
    run_daily_discovery = MagicMock(return_value=[])
    monkeypatch.setattr(parser_main, "run_daily_discovery", run_daily_discovery)

    parser_main.main()

    assert run_all.call_args.kwargs["ru_proxy_url"] == "http://proxy.example:8888"
    run_daily_discovery.assert_not_called()  # YANDEX_SEARCH_API_KEY пуст — шаг пропущен
    parser_main.get_settings.cache_clear()


def test_main_passes_none_when_ru_proxy_url_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("RU_PROXY_URL", "")
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "")
    parser_main.get_settings.cache_clear()
    monkeypatch.setattr(sys, "argv", ["parser"])

    run_all = MagicMock(return_value=[])
    monkeypatch.setattr(parser_main, "run_all", run_all)
    monkeypatch.setattr(parser_main, "run_daily_discovery", MagicMock(return_value=[]))

    parser_main.main()

    assert run_all.call_args.kwargs["ru_proxy_url"] is None
    parser_main.get_settings.cache_clear()


def test_main_calls_run_daily_discovery_when_yandex_creds_set(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "test-key")
    monkeypatch.setenv("YANDEX_SEARCH_FOLDER_ID", "test-folder")
    parser_main.get_settings.cache_clear()
    monkeypatch.setattr(sys, "argv", ["parser"])

    monkeypatch.setattr(parser_main, "run_all", MagicMock(return_value=[]))
    run_daily_discovery = MagicMock(return_value=[])
    monkeypatch.setattr(parser_main, "run_daily_discovery", run_daily_discovery)

    parser_main.main()

    assert run_daily_discovery.call_args.kwargs["api_key"] == "test-key"
    assert run_daily_discovery.call_args.kwargs["folder_id"] == "test-folder"
    parser_main.get_settings.cache_clear()


def test_main_skips_run_daily_discovery_when_yandex_creds_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("YANDEX_SEARCH_API_KEY", "")
    monkeypatch.setenv("YANDEX_SEARCH_FOLDER_ID", "")
    parser_main.get_settings.cache_clear()
    monkeypatch.setattr(sys, "argv", ["parser"])

    monkeypatch.setattr(parser_main, "run_all", MagicMock(return_value=[]))
    run_daily_discovery = MagicMock(return_value=[])
    monkeypatch.setattr(parser_main, "run_daily_discovery", run_daily_discovery)

    parser_main.main()

    run_daily_discovery.assert_not_called()
    parser_main.get_settings.cache_clear()
