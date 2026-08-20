"""Адаптер к внешнему агенту автообновления (AGENTS.md раздел 3, раздел 16 п.8).

Контракт реального агента не определён: `/Users/user/dev/auto` (LangGraph-пайплайн
`product_agent`) не поднимается как самостоятельный HTTP-сервис — либо вызывать его как
библиотеку/сабпроцесс, либо ждать задеплоенный Sberbank-эндпоинт. Выбор стратегии
отложен до Фазы 5 (AGENTS.md раздел 16 п.8). Этот класс изолирует решение за одним
методом, чтобы подключение реального контракта не требовало переделки bot/main.py —
именно это и требует критерий готовности «через адаптер, даже если контракт агента
пока не подключён» (AGENTS.md раздел 15).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class AutoUpdateAgentClient:
    """Заглушка: логирует передачу вместо реального вызова агента."""

    def send(self, npa_link: str | None, measure_id: str | None) -> None:
        log.info(
            "AutoUpdateAgentClient (заглушка, контракт не подключён): npa_link=%s measure_id=%s",
            npa_link,
            measure_id,
        )
