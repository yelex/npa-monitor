"""Конфигурация npa-monitor (pydantic-settings, PLAN.md Фаза 0/5)."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Telegram
    telegram_bot_token: str = ""
    allowed_telegram_user_ids: str = ""  # через запятую

    # БД
    database_path: str = "data/npa_monitor.db"

    # RU-прокси (STAGE0.md 2.1)
    ru_proxy_url: str = "http://95.142.42.28:8888"

    # GLM fallback (Фаза 4, опционально)
    glm_personal_api_key: str = ""
    glm_personal_base_url: str = "https://api.z.ai/api/coding/paas/v4"
    glm_personal_model: str = "glm-5.1"

    @property
    def allowed_user_ids(self) -> set[int]:
        if not self.allowed_telegram_user_ids.strip():
            return set()
        return {int(x) for x in self.allowed_telegram_user_ids.split(",") if x.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
