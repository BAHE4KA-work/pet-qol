"""Telegram Bot API 10.4/10.5 HTML Widget client and payload validation."""

from __future__ import annotations

import gzip
import json
from typing import Any

import httpx
from pydantic import BaseModel

MAX_WIDGET_HTML_BYTES = 32_768
MAX_WIDGET_GZIP_BYTES = 4_096


class WidgetPayloadError(ValueError):
    """Raised when a Telegram HTML widget payload exceeds size or compression limits."""


class WidgetAPIError(RuntimeError):
    """Raised when Telegram Widget Bot API returns an error or transport failure."""


class WidgetQueryUpdate(BaseModel):
    """Normalized Telegram `widget_query` update payload."""

    id: str
    from_user_id: int
    chat_id: int | None = None
    message_id: int | None = None
    data: str

    @classmethod
    def from_telegram_update(cls, payload: dict[str, Any]) -> WidgetQueryUpdate:
        """Parse a raw Bot API Update or `widget_query` dict into WidgetQueryUpdate."""
        query = payload.get("widget_query", payload)
        sender = query.get("from") or {}
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        return cls(
            id=str(query["id"]),
            from_user_id=int(query.get("from_user_id", sender.get("id", 0))),
            chat_id=query.get("chat_id", chat.get("id")),
            message_id=query.get("message_id", message.get("message_id")),
            data=str(query.get("data", "")),
        )


def validate_widget_payload(html: str) -> tuple[int, int]:
    """Validate HTML widget size against Bot API raw (32 KB) and gzip (4 KB) limits."""
    raw = html.encode("utf-8")
    raw_size = len(raw)
    if raw_size > MAX_WIDGET_HTML_BYTES:
        raise WidgetPayloadError(
            f"Widget HTML raw size {raw_size} bytes exceeds limit {MAX_WIDGET_HTML_BYTES}"
        )
    gzip_size = len(gzip.compress(raw))
    if gzip_size > MAX_WIDGET_GZIP_BYTES:
        raise WidgetPayloadError(
            f"Widget HTML gzip size {gzip_size} bytes exceeds limit {MAX_WIDGET_GZIP_BYTES}"
        )
    return raw_size, gzip_size


class TelegramWidgetAPI:
    """Async HTTP client for Telegram Bot API 10.4/10.5 HTML Widget endpoints."""

    def __init__(
        self,
        bot_token: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Initialize widget API client with optional shared httpx.AsyncClient."""
        self.bot_token = bot_token
        self._http_client = http_client
        self._base_url = f"https://api.telegram.org/bot{bot_token}"

    async def _post(self, method: str, payload: dict[str, Any]) -> Any:
        if not self.bot_token:
            raise WidgetAPIError("Telegram bot token is not configured")

        body = {key: value for key, value in payload.items() if value is not None}
        url = f"{self._base_url}/{method}"

        try:
            if self._http_client is not None:
                response = await self._http_client.post(url, json=body)
            else:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    response = await client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise WidgetAPIError(f"{method} transport error: {exc}") from exc

        if response.status_code != 200:
            raise WidgetAPIError(
                f"{method} failed with HTTP {response.status_code}: {response.text}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise WidgetAPIError(f"{method} returned invalid JSON") from exc

        if not isinstance(data, dict) or not data.get("ok", False):
            desc = data.get("description", "unknown error") if isinstance(data, dict) else "invalid"
            raise WidgetAPIError(f"{method} API error: {desc}")

        return data.get("result")

    async def send_widget(
        self,
        chat_id: int,
        html: str,
        *,
        allow_queries: bool = True,
        chat_data: str | None = None,
    ) -> dict[str, Any]:
        """Validate and send an interactive HTML widget message via `sendWidget`."""
        validate_widget_payload(html)
        result = await self._post(
            "sendWidget",
            {
                "chat_id": chat_id,
                "html": html,
                "allow_queries": allow_queries,
                "chat_data": chat_data,
            },
        )
        if isinstance(result, dict):
            return result
        return {"message_id": int(result)} if result is not None else {}

    async def edit_message_widget_data(
        self,
        chat_id: int,
        message_id: int,
        chat_data: str | dict[str, Any],
    ) -> dict[str, Any] | bool:
        """Update mutable widget `chat_data` in place via `editMessageWidgetData`."""
        serialized = (
            chat_data
            if isinstance(chat_data, str)
            else json.dumps(chat_data, ensure_ascii=False, separators=(",", ":"))
        )
        result = await self._post(
            "editMessageWidgetData",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "chat_data": serialized,
            },
        )
        return result if isinstance(result, (dict, bool)) else True

    async def answer_widget_query(
        self,
        widget_query_id: str,
        *,
        data: str | None = None,
    ) -> bool:
        """Acknowledge an incoming widget query via `answerWidgetQuery`."""
        result = await self._post(
            "answerWidgetQuery",
            {
                "widget_query_id": widget_query_id,
                "data": data,
            },
        )
        return bool(result) if result is not None else True
