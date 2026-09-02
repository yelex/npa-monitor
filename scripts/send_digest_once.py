"""Разовая отправка утренней сводки (docs/PLAN.md, инцидент 02.09).

Использование (внутри контейнера bot, cwd=/app):
    python scripts/send_digest_once.py          # отправить сводку всем allowed
    python scripts/send_digest_once.py --dry    # только посчитать сигналы
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram import Bot

from config import get_settings


async def main() -> None:
    dry = "--dry" in sys.argv
    settings = get_settings()

    # импорт внутри функции: bot.main тянет FSM/роутеры, нужен настроенный config
    from bot.main import _digest_signals, send_digest
    from db.session import make_engine, make_session_factory
    settings2 = get_settings()
    with make_session_factory(make_engine(settings2.database_path))() as db:
        signals = _digest_signals(db)

    if dry:
        print(f"DRY: {len(signals)} сигналов, адресаты={settings.allowed_user_ids}")
        return

    async with Bot(settings.telegram_bot_token) as bot:
        await send_digest(bot)
    print(f"OK: отправлено {len(signals)} сигналов адресатам {settings.allowed_user_ids}")


if __name__ == "__main__":
    asyncio.run(main())
