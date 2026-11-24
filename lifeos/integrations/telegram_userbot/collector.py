"""Strict read-only Telegram Userbot (Telethon) passive collector."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.utils import get_peer_id

from lifeos.config import Settings
from lifeos.db.repositories import LifeOSRepository, compute_sha256
from lifeos.logging import get_logger

logger = get_logger(__name__)

EnqueueRawItemFn = Callable[[str], Awaitable[Any] | Any]


def is_chat_allowed(chat_id: int, allowed_ids: set[int]) -> bool:
    """Enforce fail-closed whitelist check against resolved allowed chat IDs."""
    if not allowed_ids:
        return False
    return chat_id in allowed_ids


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _extract_peer_id(peer: Any) -> int | None:
    """Extract Telethon-normalized peer ID from real InputPeer/Peer or test doubles."""
    if isinstance(peer, int) and not isinstance(peer, bool):
        return peer
    try:
        return int(get_peer_id(peer))
    except Exception:
        pass
    for attr in ("chat_id", "channel_id", "user_id", "id"):
        val = getattr(peer, attr, None)
        if isinstance(val, int) and not isinstance(val, bool):
            return val
    return None


def _extract_filter_title(dialog_filter: Any) -> str:
    title = getattr(dialog_filter, "title", None)
    if title is None:
        return ""
    text = getattr(title, "text", None)
    return str(text if text is not None else title).strip()


def _extract_chat_id(event_message: Any) -> int | None:
    chat_id = getattr(event_message, "chat_id", None)
    if isinstance(chat_id, int) and not isinstance(chat_id, bool):
        return chat_id
    chat = getattr(event_message, "chat", None)
    if chat is not None:
        cid = getattr(chat, "id", None)
        if isinstance(cid, int) and not isinstance(cid, bool):
            return cid
    peer = getattr(event_message, "peer_id", None)
    if peer is not None:
        return _extract_peer_id(peer)
    return None


def _detect_media(event_message: Any) -> tuple[str, str | None]:
    """Detect modality ('audio', 'image', 'text') and MIME type from Telethon message."""
    voice = getattr(event_message, "voice", None)
    if voice:
        mime = getattr(voice, "mime_type", None) or "audio/ogg"
        return "audio", str(mime)

    doc = getattr(event_message, "document", None)
    media = getattr(event_message, "media", None)
    doc_obj = doc if doc is not None else getattr(media, "document", None)
    mime_type = getattr(doc_obj, "mime_type", None) or getattr(media, "mime_type", None)
    attributes = getattr(doc_obj, "attributes", None) or getattr(media, "attributes", None) or []

    is_voice_attr = any(bool(getattr(attr, "voice", False)) for attr in attributes)
    if is_voice_attr or mime_type == "audio/ogg":
        return "audio", str(mime_type or "audio/ogg")
    if isinstance(mime_type, str) and mime_type.startswith("audio/"):
        return "audio", mime_type

    photo = getattr(event_message, "photo", None)
    if photo:
        return "image", "image/jpeg"
    if isinstance(mime_type, str) and mime_type.startswith("image/"):
        return "image", mime_type

    return "text", None


class TelegramUserbotCollector:
    """Strict read-only passive collector listening to whitelisted Telegram chats/folders."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        enqueue_fn: EnqueueRawItemFn | None = None,
        client: Any = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.enqueue_fn = enqueue_fn
        self._client = client
        self._allowed_chat_ids: set[int] = set(settings.telethon_whitelist_chat_ids)
        self._event_handler: Any = None
        self._running = False

    def _get_or_create_client(self) -> Any:
        if self._client is not None:
            return self._client
        if (
            not self.settings.telethon_api_id
            or not self.settings.telethon_api_hash
            or not self.settings.telethon_session_string
        ):
            raise RuntimeError(
                "Telethon userbot requires telethon_api_id, telethon_api_hash, "
                "and telethon_session_string"
            )
        self._client = TelegramClient(
            StringSession(self.settings.telethon_session_string),
            self.settings.telethon_api_id,
            self.settings.telethon_api_hash,
        )
        return self._client

    async def resolve_allowed_chat_ids(self) -> set[int]:
        """Resolve allowed chat IDs from explicit config and matching Telethon folders."""
        allowed: set[int] = set(self.settings.telethon_whitelist_chat_ids)
        target_folders = {
            name.strip().lower()
            for name in self.settings.telethon_whitelist_folders
            if name.strip()
        }
        if not target_folders:
            self._allowed_chat_ids = allowed
            return allowed

        client = self._get_or_create_client()
        response = await client(GetDialogFiltersRequest())
        filters = getattr(response, "filters", response)
        if not isinstance(filters, (list, tuple)):
            filters = []

        for dialog_filter in filters:
            title = _extract_filter_title(dialog_filter).lower()
            if not title or title not in target_folders:
                continue

            included: set[int] = set()
            for peer_list_name in ("include_peers", "pinned_peers"):
                for peer in getattr(dialog_filter, peer_list_name, None) or []:
                    pid = _extract_peer_id(peer)
                    if pid is not None:
                        included.add(pid)

            excluded: set[int] = set()
            for peer in getattr(dialog_filter, "exclude_peers", None) or []:
                pid = _extract_peer_id(peer)
                if pid is not None:
                    excluded.add(pid)

            allowed.update(included - excluded)

        self._allowed_chat_ids = allowed
        return allowed

    async def _download_media_bytes(self, event_message: Any) -> bytes | None:
        download_fn = getattr(event_message, "download_media", None)
        if callable(download_fn):
            data = await _maybe_await(download_fn(file=bytes))
            if isinstance(data, (bytes, bytearray)):
                return bytes(data)
        if self._client is not None:
            client_dl = getattr(self._client, "download_media", None)
            if callable(client_dl):
                data = await _maybe_await(client_dl(event_message, file=bytes))
                if isinstance(data, (bytes, bytearray)):
                    return bytes(data)
        return None

    async def handle_incoming_message(self, event_message: Any) -> str | None:
        """Validate whitelist, extract text/media, deduplicate, store RawItem, and enqueue."""
        chat_id = _extract_chat_id(event_message)
        if chat_id is None or not is_chat_allowed(chat_id, self._allowed_chat_ids):
            return None

        raw_text_val = (
            getattr(event_message, "raw_text", None)
            or getattr(event_message, "message", None)
            or getattr(event_message, "text", None)
        )
        raw_text = str(raw_text_val).strip() if isinstance(raw_text_val, str) else None
        if raw_text == "":
            raw_text = None

        modality, media_mime_type = _detect_media(event_message)
        media_bytes: bytes | None = None
        if modality != "text":
            media_bytes = await self._download_media_bytes(event_message)
            if not media_bytes and not raw_text:
                return None

        if not raw_text and not media_bytes:
            return None

        if media_bytes is not None and raw_text:
            content_hash = compute_sha256(media_bytes + b"|" + raw_text.encode("utf-8"))
        elif media_bytes is not None:
            content_hash = compute_sha256(media_bytes)
        else:
            assert raw_text is not None
            content_hash = compute_sha256(raw_text)

        msg_id = getattr(event_message, "id", None)
        source_ref = f"tg_userbot:{chat_id}:{msg_id}" if msg_id is not None else None
        media_uri = (
            f"telethon://chat/{chat_id}/msg/{msg_id}"
            if media_bytes is not None and msg_id is not None
            else None
        )

        metadata_json: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": msg_id,
            "sender_id": getattr(event_message, "sender_id", None),
        }
        if media_bytes is not None:
            metadata_json["media_size_bytes"] = len(media_bytes)

        async with self.session_factory() as session:
            repo = LifeOSRepository(session)
            raw_item, created = await repo.create_raw_item_if_new(
                source="telegram_userbot",
                source_ref=source_ref,
                content_hash=content_hash,
                modality=modality,
                raw_text=raw_text,
                media_mime_type=media_mime_type,
                media_uri=media_uri,
                metadata_json=metadata_json,
            )
            await session.commit()

        if not created:
            return None

        if self.enqueue_fn is not None:
            await _maybe_await(self.enqueue_fn(raw_item.id))

        return raw_item.id

    async def _on_new_message(self, event: Any) -> None:
        message = getattr(event, "message", event)
        await self.handle_incoming_message(message)

    async def start_listening(self) -> None:
        """Connect Telethon client in read-only mode, resolve folders, and listen."""
        client = self._get_or_create_client()
        is_connected_fn = getattr(client, "is_connected", None)
        if callable(is_connected_fn) and not is_connected_fn():
            await _maybe_await(client.connect())
        elif hasattr(client, "connect") and not callable(is_connected_fn):
            await _maybe_await(client.connect())

        await self.resolve_allowed_chat_ids()
        self._event_handler = self._on_new_message
        client.add_event_handler(self._event_handler, events.NewMessage())
        self._running = True
        logger.info(
            "TelegramUserbotCollector listening read-only on %d whitelisted chats",
            len(self._allowed_chat_ids),
        )

        run_until_disc = getattr(client, "run_until_disconnected", None)
        if callable(run_until_disc):
            await _maybe_await(run_until_disc())

    async def stop(self) -> None:
        """Detach read-only event handler and disconnect Telethon client."""
        self._running = False
        if self._client is None:
            return
        if self._event_handler is not None:
            remove_fn = getattr(self._client, "remove_event_handler", None)
            if callable(remove_fn):
                remove_fn(self._event_handler)
            self._event_handler = None
        disconnect_fn = getattr(self._client, "disconnect", None)
        if callable(disconnect_fn):
            await _maybe_await(disconnect_fn())
