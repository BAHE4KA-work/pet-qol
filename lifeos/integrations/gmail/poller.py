"""Strict read-only Gmail poller using only gmail.readonly OAuth scope."""

from __future__ import annotations

import asyncio
import base64
import html
import inspect
import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lifeos.config import Settings
from lifeos.db.repositories import LifeOSRepository, compute_sha256
from lifeos.logging import get_logger

logger = get_logger(__name__)

GMAIL_SCOPES: list[str] = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_HISTORY_STATE_KEY = "gmail_history_id"

EnqueueRawItemFn = Callable[[str], Awaitable[Any] | Any]

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+\n")


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _execute_request(request: Any) -> dict[str, Any]:
    """Execute a googleapiclient HttpRequest off the event loop, or accept mock dicts."""
    if isinstance(request, dict):
        return request
    if inspect.isawaitable(request):
        res = await request
        return res if isinstance(res, dict) else {}
    execute_fn = getattr(request, "execute", None)
    if callable(execute_fn):
        if inspect.iscoroutinefunction(execute_fn):
            res = await execute_fn()
        else:
            res = await asyncio.to_thread(execute_fn)
        if inspect.isawaitable(res):
            res = await res
        return res if isinstance(res, dict) else {}
    return {}


def _decode_base64url(encoded: str | None) -> str:
    """Safely decode Gmail base64url strings with missing padding."""
    if not encoded:
        return ""
    padded = encoded + ("=" * (-len(encoded) % 4))
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    return raw.decode("utf-8", errors="replace")


def _strip_html(raw_html: str) -> str:
    text = _HTML_TAG_RE.sub(" ", raw_html)
    text = html.unescape(text)
    return _WHITESPACE_RE.sub("\n", text).strip()


def _extract_body_text(payload: dict[str, Any] | None) -> str:
    """Recursively extract plain-text email body from a Gmail MIME payload."""
    if not isinstance(payload, dict):
        return ""

    plain_parts: list[str] = []
    html_parts: list[str] = []

    def _walk(node: dict[str, Any]) -> None:
        mime_type = str(node.get("mimeType") or "").lower()
        body = node.get("body") or {}
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, str) and data:
            decoded = _decode_base64url(data).strip()
            if decoded:
                if mime_type == "text/plain" or not mime_type:
                    plain_parts.append(decoded)
                elif mime_type == "text/html":
                    html_parts.append(_strip_html(decoded))

        for child in node.get("parts") or []:
            if isinstance(child, dict):
                _walk(child)

    _walk(payload)
    if plain_parts:
        return "\n\n".join(plain_parts).strip()
    if html_parts:
        return "\n\n".join(html_parts).strip()
    return ""


def _extract_headers(payload: dict[str, Any] | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if not isinstance(payload, dict):
        return headers
    for entry in payload.get("headers") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip().lower()
        value = str(entry.get("value") or "").strip()
        if name:
            headers[name] = value
    return headers


def _pick_higher_history_id(current: str | None, candidate: str | None) -> str | None:
    if not candidate:
        return current
    if not current:
        return candidate
    if current.isdigit() and candidate.isdigit():
        return candidate if int(candidate) >= int(current) else current
    return candidate


class GmailReadOnlyPoller:
    """Strict read-only Gmail message poller persisting RawItems and SyncState cursor."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        enqueue_fn: EnqueueRawItemFn | None = None,
        service: Any = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.enqueue_fn = enqueue_fn
        self._service = service

    def _load_token_info(self) -> dict[str, Any]:
        raw = self.settings.gmail_token_json.strip()
        if not raw:
            raise RuntimeError("Gmail read-only poller requires settings.gmail_token_json")
        if raw.startswith("{"):
            data = json.loads(raw)
        else:
            path = Path(raw)
            if not path.exists():
                raise RuntimeError(f"Gmail token file not found: {raw}")
            data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("Invalid gmail_token_json payload")
        return data

    def _build_service_sync(self) -> Any:
        info = self._load_token_info()
        creds = Credentials.from_authorized_user_info(info, scopes=GMAIL_SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    async def _get_service(self) -> Any:
        if self._service is None:
            self._service = await asyncio.to_thread(self._build_service_sync)
        return self._service

    async def _list_message_stubs(self, service: Any) -> list[dict[str, Any]]:
        stubs: list[dict[str, Any]] = []
        page_token: str | None = None
        messages_resource = service.users().messages()

        while True:
            kwargs: dict[str, Any] = {
                "userId": "me",
                "q": self.settings.gmail_poll_query,
            }
            if page_token is not None:
                kwargs["pageToken"] = page_token
            resp = await _execute_request(messages_resource.list(**kwargs))
            stubs.extend(resp.get("messages") or [])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        return stubs

    async def poll_once(self) -> list[str]:
        """Poll Gmail read-only, deduplicate by threadId/id/SHA-256, and enqueue new items."""
        service = await self._get_service()
        messages_resource = service.users().messages()

        async with self.session_factory() as session:
            repo = LifeOSRepository(session)
            cursor_state = await repo.get_sync_state(GMAIL_HISTORY_STATE_KEY)
            prev_history_id = (
                str(cursor_state["history_id"])
                if isinstance(cursor_state, dict) and cursor_state.get("history_id")
                else None
            )

            stubs = await self._list_message_stubs(service)
            new_item_ids: list[str] = []
            max_history_id = prev_history_id
            seen_refs: set[str] = set()

            for stub in stubs:
                msg_id = str(stub.get("id") or "").strip()
                if not msg_id:
                    continue

                full_msg = await _execute_request(
                    messages_resource.get(userId="me", id=msg_id, format="full")
                )
                resolved_id = str(full_msg.get("id") or msg_id).strip()
                thread_id = str(
                    full_msg.get("threadId") or stub.get("threadId") or resolved_id
                ).strip()

                source_ref = f"gmail:{thread_id}:{resolved_id}"
                if source_ref in seen_refs:
                    continue
                seen_refs.add(source_ref)

                msg_history_id = full_msg.get("historyId")
                if msg_history_id is not None:
                    max_history_id = _pick_higher_history_id(max_history_id, str(msg_history_id))

                payload = full_msg.get("payload") or {}
                headers = _extract_headers(payload)
                subject = headers.get("subject", "(no subject)")
                sender = headers.get("from", "")
                body_text = (
                    _extract_body_text(payload)
                    or str(full_msg.get("snippet") or "").strip()
                )

                lines = [f"Subject: {subject}"]
                if sender:
                    lines.append(f"From: {sender}")
                if body_text:
                    lines.append("")
                    lines.append(body_text)
                raw_text = "\n".join(lines).strip()
                content_hash = compute_sha256(raw_text)

                raw_item, created = await repo.create_raw_item_if_new(
                    source="gmail",
                    source_ref=source_ref,
                    content_hash=content_hash,
                    modality="email",
                    raw_text=raw_text,
                    metadata_json={
                        "thread_id": thread_id,
                        "message_id": resolved_id,
                        "subject": subject,
                        "sender": sender,
                        "history_id": str(msg_history_id) if msg_history_id is not None else None,
                        "source_link": f"https://mail.google.com/mail/u/0/#inbox/{thread_id}",
                    },
                )
                if created:
                    new_item_ids.append(raw_item.id)

            if max_history_id and max_history_id != prev_history_id:
                await repo.set_sync_state(
                    GMAIL_HISTORY_STATE_KEY,
                    {"history_id": max_history_id},
                )

            await session.commit()

        if self.enqueue_fn is not None:
            for item_id in new_item_ids:
                await _maybe_await(self.enqueue_fn(item_id))

        return new_item_ids
