"""Точка входа для `python -m bot` (см. докстринг bot/main.py)."""
import asyncio

from bot.main import main

if __name__ == "__main__":
    asyncio.run(main())
