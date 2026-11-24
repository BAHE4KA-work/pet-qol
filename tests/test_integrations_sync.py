"""Tests for Notion, Apple iCloud CalDAV, Telegram Userbot, and Gmail integrations."""

from __future__ import annotations

import ast
import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lifeos.config import Settings
from lifeos.db.base import Base, create_engine_and_sessionmaker
from lifeos.db.models import RawItem, Task
from lifeos.db.repositories import LifeOSRepository, compute_sha256
from lifeos.integrations.caldav import ICloudCalDAVService
from lifeos.integrations.gmail import GMAIL_SCOPES, GmailReadOnlyPoller
from lifeos.integrations.notion import NotionSyncService
from lifeos.integrations.telegram_userbot import (
    TelegramUserbotCollector,
    is_chat_allowed,
)


@pytest.fixture
async def db_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Provide an isolated in-memory async SQLite session factory."""
    engine, session_factory = create_engine_and_sessionmaker("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield session_factory
    await engine.dispose()


# --- Notion 5-DB Sync Tests ---


async def test_notion_bootstrap_push_and_pull() -> None:
    """NotionSyncService bootstraps 5 DBs, pushes tasks, and pulls recent changes."""
    created_db_ids = ["db-goals", "db-projects", "db-tasks", "db-telemetry", "db-insights"]
    mock_client = SimpleNamespace(
        databases=SimpleNamespace(
            create=AsyncMock(side_effect=[{"id": db_id} for db_id in created_db_ids]),
            update=AsyncMock(return_value={"id": "db-tasks"}),
            query=AsyncMock(
                side_effect=[
                    {
                        "results": [
                            {
                                "id": "goal-page-1",
                                "last_edited_time": "2026-11-24T10:00:00.000Z",
                                "properties": {
                                    "Name": {"title": [{"plain_text": "Launch LifeOS"}]},
                                    "Horizon": {"select": {"name": "quarter"}},
                                    "Priority": {"number": 1},
                                    "Progress %": {"number": 0.45},
                                    "Status": {"select": {"name": "active"}},
                                },
                            }
                        ],
                        "has_more": False,
                    },
                    {
                        "results": [
                            {
                                "id": "task-page-1",
                                "last_edited_time": "2026-11-24T11:00:00.000Z",
                                "properties": {
                                    "Name": {"title": [{"plain_text": "Ship passive collectors"}]},
                                    "LifeOS ID": {"rich_text": [{"plain_text": "task-uuid-1"}]},
                                    "Status": {"select": {"name": "Active"}},
                                    "Confidence": {"number": 0.91},
                                    "Energy": {"select": {"name": "deep"}},
                                    "Effort": {"number": 6.0},
                                    "Day Anchor": {"select": {"name": "morning"}},
                                    "Deferral Count": {"number": 1},
                                    "DoD": {"rich_text": [{"plain_text": "pytest green"}]},
                                    "Source Link": {"url": "https://t.me/c/1/2"},
                                },
                            }
                        ],
                        "has_more": False,
                    },
                ]
            ),
        ),
        pages=SimpleNamespace(
            create=AsyncMock(return_value={"id": "new-task-page-99"}),
            update=AsyncMock(return_value={"id": "existing-page-42"}),
        ),
    )

    settings = Settings(
        notion_token="secret-notion",
        notion_goals_db_id="db-goals",
        notion_tasks_db_id="db-tasks",
    )
    svc = NotionSyncService(settings=settings, client=mock_client)

    bootstrapped = await svc.bootstrap_databases(parent_page_id="parent-page-1")
    assert bootstrapped == {
        "goals": "db-goals",
        "projects": "db-projects",
        "tasks": "db-tasks",
        "telemetry": "db-telemetry",
        "insights": "db-insights",
    }
    assert mock_client.databases.create.await_count == 5
    mock_client.databases.update.assert_awaited_once()
    update_kwargs = mock_client.databases.update.await_args.kwargs
    assert "Parent Task" in update_kwargs["properties"]

    new_task = Task(
        id="task-1",
        title="Review architecture",
        dod="All checks pass",
        status="active",
        confidence=0.95,
        energy_tier="deep",
        effort=6.0,
        day_anchor="morning",
    )
    created_page_id = await svc.push_task(new_task, goal_notion_id="goal-page-1")
    assert created_page_id == "new-task-page-99"
    mock_client.pages.create.assert_awaited_once()

    existing_task = Task(
        id="task-2",
        notion_page_id="existing-page-42",
        title="Existing task",
        dod="Done",
        status="done",
    )
    updated_page_id = await svc.push_task(existing_task)
    assert updated_page_id == "existing-page-42"
    mock_client.pages.update.assert_awaited_once()

    changes = await svc.pull_recent_changes(since=datetime(2026, 11, 24, 0, 0, tzinfo=UTC))
    assert len(changes["goals"]) == 1
    assert changes["goals"][0]["title"] == "Launch LifeOS"
    assert len(changes["tasks"]) == 1
    assert changes["tasks"][0]["lifeos_id"] == "task-uuid-1"
    assert changes["tasks"][0]["status"] == "active"
    assert changes["tasks"][0]["energy_tier"] == "deep"


# --- Apple iCloud CalDAV Tests ---


async def test_caldav_fetch_busy_slots_and_event_only_guard() -> None:
    """ICloudCalDAVService parses VEVENTs and blocks non-event writes with ValueError."""
    ics_payload = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Test//Test//EN\r\n"
        "BEGIN:VEVENT\r\n"
        "UID:evt-101\r\n"
        "DTSTART;TZID=UTC:20261124T100000Z\r\n"
        "DTEND;TZID=UTC:20261124T113000Z\r\n"
        "SUMMARY:Deep Focus Sync\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    mock_calendar = MagicMock()
    mock_calendar.name = "LifeOS"
    mock_calendar.search.return_value = [SimpleNamespace(data=ics_payload)]

    mock_principal = MagicMock()
    mock_principal.calendars.return_value = [mock_calendar]

    mock_dav_client = MagicMock()
    mock_dav_client.principal.return_value = mock_principal

    service = ICloudCalDAVService(settings=Settings(timezone="UTC"), caldav_client=mock_dav_client)
    slots = await service.fetch_busy_slots(
        datetime(2026, 11, 24, 0, 0, tzinfo=UTC),
        datetime(2026, 11, 25, 0, 0, tzinfo=UTC),
    )
    assert len(slots) == 1
    assert slots[0].uid == "evt-101"
    assert slots[0].title == "Deep Focus Sync"
    assert slots[0].duration_hours == pytest.approx(1.5)

    non_event_task = Task(
        id="task-no-cal",
        title="Write docs",
        dod="README updated",
        item_type="task",
        due_at=datetime(2026, 11, 24, 14, 0, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="item_type='event'"):
        await service.upsert_event(non_event_task)
    mock_calendar.save_event.assert_not_called()

    event_task = Task(
        id="event-1",
        title="Dentist Appointment",
        dod="Checkup complete",
        item_type="event",
        duration_bucket=3,
        due_at=datetime(2026, 11, 24, 15, 0, tzinfo=UTC),
    )
    uid = await service.upsert_event(event_task)
    assert uid == "event-1"
    mock_calendar.save_event.assert_called_once()
    saved_ical = mock_calendar.save_event.call_args.args[0]
    assert "BEGIN:VEVENT" in saved_ical
    assert "SUMMARY:Dentist Appointment" in saved_ical


# --- Telegram Userbot Collector Tests ---


async def test_userbot_whitelist_voice_ogg_ingestion_and_dedup(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Userbot enforces whitelist/folders, ingests .ogg voice notes, and deduplicates."""
    assert is_chat_allowed(100, set()) is False
    assert is_chat_allowed(100, {100, 200}) is True
    assert is_chat_allowed(999, {100, 200}) is False

    work_filter = SimpleNamespace(
        title=SimpleNamespace(text="Work"),
        include_peers=[SimpleNamespace(channel_id=300)],
        pinned_peers=[],
        exclude_peers=[],
    )
    ignored_filter = SimpleNamespace(
        title="Spam",
        include_peers=[SimpleNamespace(channel_id=999)],
        pinned_peers=[],
        exclude_peers=[],
    )
    mock_client = AsyncMock(
        return_value=SimpleNamespace(filters=[work_filter, ignored_filter])
    )

    enqueued: list[str] = []

    async def fake_enqueue(raw_item_id: str) -> None:
        enqueued.append(raw_item_id)

    settings = Settings(
        telethon_whitelist_chat_ids=[100],
        telethon_whitelist_folders=["Work"],
    )
    collector = TelegramUserbotCollector(
        settings=settings,
        session_factory=db_session_factory,
        enqueue_fn=fake_enqueue,
        client=mock_client,
    )

    resolved = await collector.resolve_allowed_chat_ids()
    assert resolved == {100, 300}

    blocked_msg = SimpleNamespace(
        id=10,
        chat_id=999,
        sender_id=42,
        raw_text="Ignore this spam message",
        voice=None,
        photo=None,
        document=None,
        media=None,
    )
    assert await collector.handle_incoming_message(blocked_msg) is None
    assert enqueued == []

    ogg_bytes = b"OggS\x00\x02fake-opus-voice-payload"
    voice_msg = SimpleNamespace(
        id=55,
        chat_id=300,
        sender_id=77,
        raw_text=None,
        message=None,
        text=None,
        voice=SimpleNamespace(mime_type="audio/ogg"),
        photo=None,
        document=None,
        media=None,
        download_media=AsyncMock(return_value=ogg_bytes),
    )

    created_id = await collector.handle_incoming_message(voice_msg)
    assert created_id is not None
    assert enqueued == [created_id]

    # Duplicate message / identical .ogg payload must not create a second RawItem.
    dup_id = await collector.handle_incoming_message(voice_msg)
    assert dup_id is None
    assert enqueued == [created_id]

    async with db_session_factory() as session:
        rows = list((await session.execute(select(RawItem))).scalars().all())
        assert len(rows) == 1
        assert rows[0].source == "telegram_userbot"
        assert rows[0].modality == "audio"
        assert rows[0].media_mime_type == "audio/ogg"
        assert rows[0].content_hash == compute_sha256(ogg_bytes)
        assert rows[0].source_ref == "tg_userbot:300:55"


def test_userbot_collector_ast_strict_read_only() -> None:
    """AST scan guarantees collector.py never calls outbound send/edit/delete/forward methods."""
    collector_path = (
        Path(__file__).resolve().parent.parent
        / "lifeos"
        / "integrations"
        / "telegram_userbot"
        / "collector.py"
    )
    source = collector_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(collector_path))

    forbidden_methods = {
        "send_message",
        "send_file",
        "edit_message",
        "delete_messages",
        "forward_messages",
        "send_read_acknowledge",
        "pin_message",
        "unpin_message",
        "send_reaction",
        "edit_folder",
        "create_chat",
        "add_chat_user",
        "kick_participant",
    }

    called_attrs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                called_attrs.add(node.func.attr)
            elif isinstance(node.func, ast.Name):
                called_attrs.add(node.func.id)

    violations = called_attrs & forbidden_methods
    assert not violations, f"Forbidden Telethon write methods detected: {violations}"


# --- Gmail Read-Only Poller Tests ---


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


async def test_gmail_readonly_scope_dedup_and_cursor_persistence(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """GmailReadOnlyPoller enforces readonly scope, decodes body, dedups, and saves cursor."""
    assert GMAIL_SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]

    msg_1_full = {
        "id": "msg-1",
        "threadId": "thr-1",
        "historyId": "1050",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": "Quarterly roadmap review"},
                {"name": "From", "value": "alex@example.com"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": _b64url("Please prepare the Q4 slides by Friday.")},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": _b64url("<p>Please prepare the Q4 slides.</p>")},
                },
            ],
        },
    }
    msg_2_full = {
        "id": "msg-2",
        "threadId": "thr-1",
        "historyId": "1099",
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": "Re: Quarterly roadmap review"},
                {"name": "From", "value": "sam@example.com"},
            ],
            "body": {"data": _b64url("Confirmed, I will send the budget numbers.")},
        },
    }

    list_responses = [
        {"messages": [{"id": "msg-1", "threadId": "thr-1"}]},
        {"messages": [{"id": "msg-1", "threadId": "thr-1"}, {"id": "msg-2", "threadId": "thr-1"}]},
    ]
    full_map = {"msg-1": msg_1_full, "msg-2": msg_2_full}

    messages_resource = MagicMock()
    messages_resource.list.side_effect = [
        MagicMock(execute=MagicMock(return_value=resp)) for resp in list_responses
    ]
    messages_resource.get.side_effect = lambda userId, id, format: MagicMock(
        execute=MagicMock(return_value=full_map[id])
    )
    service = MagicMock()
    service.users.return_value.messages.return_value = messages_resource

    enqueued: list[str] = []

    async def fake_enqueue(raw_item_id: str) -> None:
        enqueued.append(raw_item_id)

    poller = GmailReadOnlyPoller(
        settings=Settings(gmail_poll_query="is:unread newer_than:7d"),
        session_factory=db_session_factory,
        enqueue_fn=fake_enqueue,
        service=service,
    )

    first_ids = await poller.poll_once()
    assert len(first_ids) == 1
    assert enqueued == first_ids

    async with db_session_factory() as session:
        repo = LifeOSRepository(session)
        state = await repo.get_sync_state("gmail_history_id")
        assert state == {"history_id": "1050"}
        items = list((await session.execute(select(RawItem))).scalars().all())
        assert len(items) == 1
        assert items[0].source == "gmail"
        assert items[0].modality == "email"
        assert items[0].source_ref == "gmail:thr-1:msg-1"
        assert "Quarterly roadmap review" in (items[0].raw_text or "")
        assert "Please prepare the Q4 slides by Friday." in (items[0].raw_text or "")

    # Second poll returns msg-1 (duplicate) + msg-2 (new message in same thread).
    second_ids = await poller.poll_once()
    assert len(second_ids) == 1
    assert enqueued == first_ids + second_ids

    async with db_session_factory() as session:
        repo = LifeOSRepository(session)
        state = await repo.get_sync_state("gmail_history_id")
        assert state == {"history_id": "1099"}
        items = list((await session.execute(select(RawItem))).scalars().all())
        assert len(items) == 2
        refs = {item.source_ref for item in items}
        assert refs == {"gmail:thr-1:msg-1", "gmail:thr-1:msg-2"}
