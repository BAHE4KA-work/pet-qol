"""Telegram Bot API 10.4/10.5 HTML Widget integration and fallback inline cards."""

from __future__ import annotations

from lifeos.integrations.telegram_bot.bot import (
    LifeOSTelegramBot,
    is_authorized_user,
)
from lifeos.integrations.telegram_bot.templates import (
    render_today_fallback_message,
    render_today_widget_html,
    render_today_widget_state,
)
from lifeos.integrations.telegram_bot.widgets import (
    MAX_WIDGET_GZIP_BYTES,
    MAX_WIDGET_HTML_BYTES,
    TelegramWidgetAPI,
    WidgetAPIError,
    WidgetPayloadError,
    WidgetQueryUpdate,
    validate_widget_payload,
)

__all__ = [
    "LifeOSTelegramBot",
    "MAX_WIDGET_GZIP_BYTES",
    "MAX_WIDGET_HTML_BYTES",
    "TelegramWidgetAPI",
    "WidgetAPIError",
    "WidgetPayloadError",
    "WidgetQueryUpdate",
    "is_authorized_user",
    "render_today_fallback_message",
    "render_today_widget_html",
    "render_today_widget_state",
    "validate_widget_payload",
]
