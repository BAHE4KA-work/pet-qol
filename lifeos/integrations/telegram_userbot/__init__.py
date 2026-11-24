"""Strict read-only Telegram Userbot (Telethon) collector package."""

from __future__ import annotations

from lifeos.integrations.telegram_userbot.collector import (
    TelegramUserbotCollector,
    is_chat_allowed,
)

__all__ = ["TelegramUserbotCollector", "is_chat_allowed"]
