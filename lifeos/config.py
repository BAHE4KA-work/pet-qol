"""Application settings for LifeOS."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralized environment configuration loaded from .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App & Security
    app_env: str = "development"
    api_bearer_token: str = "dev-insecure-token-change-me"
    encryption_key: str = ""
    timezone: str = "Europe/Moscow"

    # Database & Redis
    database_url: str = "postgresql+asyncpg://lifeos:lifeos@postgres:5432/lifeos"
    redis_url: str = "redis://redis:6379/0"

    # Google Gemini & NVIDIA NIM fallback
    gemini_api_key: str = ""
    embedding_model: str = "gemini-embedding-2"
    embedding_dim: int = 768
    decision_model: str = "gemini-decision-1"
    stt_model: str = "gemini-3.5-transcribe"
    tier2_model: str = "gemma-4-31b-it"
    tier3_model: str = "gemini-4-hydrogen"
    nvidia_nim_api_key: str = ""
    nvidia_nim_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_nim_model: str = "google/gemma-4-31b-it"

    # Quotas & Goldilocks Controller
    hydrogen_daily_limit: int = 20
    hydrogen_reserved_system: int = 3
    high_confidence_threshold: float = 0.78
    medium_confidence_threshold: float = 0.45
    base_daily_capacity: float = 20.0
    goldilocks_low: float = 0.75
    goldilocks_high: float = 0.85
    deferral_split_threshold: int = 3

    # Notion (5 databases)
    notion_token: str = ""
    notion_parent_page_id: str = ""
    notion_goals_db_id: str = ""
    notion_projects_db_id: str = ""
    notion_tasks_db_id: str = ""
    notion_telemetry_db_id: str = ""
    notion_insights_db_id: str = ""

    # Apple iCloud CalDAV
    caldav_url: str = "https://caldav.icloud.com/"
    caldav_username: str = ""
    caldav_app_password: str = ""
    caldav_calendar_name: str = "LifeOS"

    # Telegram Bot
    telegram_bot_token: str = ""
    telegram_allowed_user_id: int = 0
    telegram_widget_enabled: bool = True

    # Telegram Userbot (strict read-only)
    telethon_api_id: int = 0
    telethon_api_hash: str = ""
    telethon_session_string: str = ""
    telethon_whitelist_chat_ids: list[int] = Field(default_factory=list)
    telethon_whitelist_folders: list[str] = Field(default_factory=list)

    # Gmail (strict read-only)
    gmail_credentials_json: str = ""
    gmail_token_json: str = ""
    gmail_poll_query: str = "is:unread newer_than:7d"


@lru_cache
def get_settings() -> Settings:
    """Return cached Settings instance."""
    return Settings()
