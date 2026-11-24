"""Tests for Telegram Bot API 10.4/10.5 HTML widgets, fallback cards, and ACL."""

from __future__ import annotations

import base64
import gzip
import json
import random
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lifeos.config import Settings
from lifeos.db.base import Base, create_engine_and_sessionmaker
from lifeos.db.models import RawItem, Task
from lifeos.db.repositories import LifeOSRepository
from lifeos.integrations.telegram_bot import (
    MAX_WIDGET_GZIP_BYTES,
    MAX_WIDGET_HTML_BYTES,
    LifeOSTelegramBot,
    TelegramWidgetAPI,
    WidgetAPIError,
    WidgetPayloadError,
    WidgetQueryUpdate,
    is_authorized_user,
    render_today_fallback_message,
    render_today_widget_html,
    validate_widget_payload,
)
from lifeos.schemas.planner import (
    BudgetBreakdown,
    CalendarSlot,
    CandidateItemDTO,
    DailyPlanView,
    TaskCardDTO,
)


@pytest.fixture
async def db_session_factory() -> async_sessionmaker[AsyncSession]:
    """Provide an isolated in-memory async SQLite session factory."""
    engine, session_factory = create_engine_and_sessionmaker("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield session_factory
    await engine.dispose()


def _sample_plan(num_tasks: int = 12, num_candidates: int = 3) -> DailyPlanView:
    today = date(2026, 11, 24)
    anchors = ("morning", "afternoon", "evening")
    morning: list[TaskCardDTO] = []
    afternoon: list[TaskCardDTO] = []
    evening: list[TaskCardDTO] = []

    for idx in range(num_tasks):
        anchor = anchors[idx % 3]
        card = TaskCardDTO(
            id=f"task-{idx:02d}",
            title=f"Задача {idx + 1}: подготовить отчет и сверить метрики",
            dod="Отчет отправлен",
            item_type="task",
            status="done" if idx % 4 == 0 else "active",
            confidence=0.92,
            confidence_tier="high",
            energy_tier="deep" if idx % 3 == 0 else "medium",
            effort=3.0,
            day_anchor=anchor,  # type: ignore[arg-type]
            scheduled_date=today,
            due_at=datetime(2026, 11, 24, 10, 0, tzinfo=UTC),
            has_exact_time=(idx == 0),
        )
        if anchor == "morning":
            morning.append(card)
        elif anchor == "afternoon":
            afternoon.append(card)
        else:
            evening.append(card)

    candidates = [
        CandidateItemDTO(
            id=f"cand-{idx:02d}",
            title=f"Вероятное дело {idx + 1}: позвонить врачу",
            dod="Запись подтверждена",
            confidence=0.63,
            energy_tier="low",
            effort=2.0,
        )
        for idx in range(num_candidates)
    ]

    return DailyPlanView(
        day=today,
        budget=BudgetBreakdown(
            base_capacity=20.0,
            k_calendar=0.85,
            k_health=0.95,
            k_velocity=1.0,
            daily_budget=16.15,
            strip_deep_tasks=False,
        ),
        effort_planned=15.0,
        effort_done=6.0,
        morning=morning,
        afternoon=afternoon,
        evening=evening,
        candidates=candidates,
        calendar_events=[
            CalendarSlot(
                uid="cal-1",
                title="Sync",
                start_at=datetime(2026, 11, 24, 11, 0, tzinfo=UTC),
                end_at=datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
            )
        ],
    )


def test_validate_widget_payload_limits() -> None:
    """Validate raw 32 KB and gzip 4 KB bounds."""
    raw_len, gz_len = validate_widget_payload("<html><body>ok</body></html>")
    assert raw_len <= MAX_WIDGET_HTML_BYTES
    assert gz_len <= MAX_WIDGET_GZIP_BYTES

    too_big_raw = "a" * (MAX_WIDGET_HTML_BYTES + 1)
    with pytest.raises(WidgetPayloadError, match="raw size"):
        validate_widget_payload(too_big_raw)

    # Generate incompressible ASCII under 32 KB that exceeds 4 KB gzipped.
    raw_bytes = random.Random(42).randbytes(6000)
    incompressible = base64.b64encode(raw_bytes).decode("ascii")
    assert len(incompressible.encode("utf-8")) < MAX_WIDGET_HTML_BYTES
    assert len(gzip.compress(incompressible.encode("utf-8"))) > MAX_WIDGET_GZIP_BYTES
    with pytest.raises(WidgetPayloadError, match="gzip size"):
        validate_widget_payload(incompressible)


def test_render_today_widget_html_compact_and_complete() -> None:
    """Today HTML widget stays under 4 KB gzipped with 15 items and exposes 1-tap actions."""
    plan = _sample_plan(num_tasks=12, num_candidates=4)
    html = render_today_widget_html(plan)
    raw_size, gz_size = validate_widget_payload(html)

    assert raw_size <= MAX_WIDGET_HTML_BYTES
    assert gz_size <= MAX_WIDGET_GZIP_BYTES
    assert "Утро" in html
    assert "День" in html
    assert "Вечер" in html
    assert "Вероятные дела" in html
    assert "6/16.15" in html
    assert "Telegram.Widget.sendQuery" in html
    assert 'data-a="t"' in html
    assert 'data-a="y"' in html
    assert 'data-a="n"' in html


def test_render_today_fallback_message() -> None:
    """Fallback renderer builds Telegram HTML summary and InlineKeyboardMarkup callbacks."""
    plan = _sample_plan(num_tasks=3, num_candidates=2)
    text, markup = render_today_fallback_message(plan)

    assert isinstance(markup, InlineKeyboardMarkup)
    assert "2026-11-24" in text
    assert "Вероятные дела" in text
    callbacks = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "t:task-00" in callbacks
    assert "y:cand-00" in callbacks
    assert "n:cand-00" in callbacks


async def test_telegram_widget_api_calls_and_errors() -> None:
    """TelegramWidgetAPI calls Bot API 10.4 endpoints and raises WidgetAPIError on failures."""
    token = "123456:TESTTOKEN"
    api = TelegramWidgetAPI(bot_token=token)

    with respx.mock(assert_all_called=True) as mock_http:
        send_route = mock_http.post(f"https://api.telegram.org/bot{token}/sendWidget").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})
        )
        edit_route = mock_http.post(
            f"https://api.telegram.org/bot{token}/editMessageWidgetData"
        ).mock(return_value=httpx.Response(200, json={"ok": True, "result": True}))
        ans_route = mock_http.post(f"https://api.telegram.org/bot{token}/answerWidgetQuery").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": True})
        )

        res = await api.send_widget(chat_id=999, html="<html><body>Hi</body></html>")
        assert res["message_id"] == 77
        assert send_route.called

        edited = await api.edit_message_widget_data(chat_id=999, message_id=77, chat_data={"x": 1})
        assert edited is True
        assert edit_route.called

        answered = await api.answer_widget_query(widget_query_id="wq-1", data="ok")
        assert answered is True
        assert ans_route.called

    with respx.mock as mock_err:
        mock_err.post(f"https://api.telegram.org/bot{token}/sendWidget").mock(
            return_value=httpx.Response(400, json={"ok": False, "description": "Bad Request"})
        )
        with pytest.raises(WidgetAPIError, match="HTTP 400"):
            await api.send_widget(chat_id=999, html="<html><body>Hi</body></html>")


async def test_send_or_update_widget_and_fallback_on_api_error(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Bot persists widget message_id, edits in place, and falls back on WidgetAPIError."""
    settings = Settings(
        telegram_bot_token="123456:TESTTOKEN",
        telegram_allowed_user_id=4242,
        telegram_widget_enabled=True,
    )
    widget_api = AsyncMock(spec=TelegramWidgetAPI)
    widget_api.send_widget.return_value = {"message_id": 101}
    widget_api.edit_message_widget_data.return_value = True

    fake_bot = AsyncMock()
    fake_bot.send_message.return_value = SimpleNamespace(message_id=202)

    tg_bot = LifeOSTelegramBot(
        settings=settings,
        session_factory=db_session_factory,
        widget_api=widget_api,
        bot=fake_bot,
    )
    plan = _sample_plan(num_tasks=3, num_candidates=1)

    first_id = await tg_bot.send_or_update_today_widget(chat_id=4242, plan=plan)
    assert first_id == 101
    widget_api.send_widget.assert_awaited_once()

    async with db_session_factory() as session:
        repo = LifeOSRepository(session)
        state = await repo.get_sync_state("tg_widget_message_id")
        assert state is not None
        assert state["message_id"] == 101
        assert state["mode"] == "widget"

    second_id = await tg_bot.send_or_update_today_widget(chat_id=4242, plan=plan)
    assert second_id == 101
    widget_api.edit_message_widget_data.assert_awaited_once()

    # Now simulate WidgetAPIError -> automatic fallback to InlineKeyboard message.
    widget_api.edit_message_widget_data.side_effect = WidgetAPIError("Widgets disabled")
    fallback_id = await tg_bot.send_or_update_today_widget(chat_id=4242, plan=plan)
    assert fallback_id == 202
    fake_bot.send_message.assert_awaited_once()
    call_kwargs = fake_bot.send_message.await_args.kwargs
    assert isinstance(call_kwargs["reply_markup"], InlineKeyboardMarkup)


def test_is_authorized_user_fail_closed() -> None:
    """Single-user ACL rejects None, 0 allowed_user_id, and mismatched user IDs."""
    assert is_authorized_user(1001, 0) is False
    assert is_authorized_user(None, 1001) is False
    assert is_authorized_user(9999, 1001) is False
    assert is_authorized_user(1001, 1001) is True


async def test_widget_query_toggle_approve_dismiss_and_acl(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """1-tap widget queries enforce ACL, mutate tasks/candidates, and refresh widget."""
    today = date.today()
    async with db_session_factory() as session:
        repo = LifeOSRepository(session)
        task = await repo.create_task(
            title="Написать тесты",
            dod="pytest green",
            status="active",
            scheduled_date=today,
            day_anchor="morning",
        )
        cand_yes = await repo.create_task(
            title="Кандидат 1",
            dod="Проверить почту",
            status="candidate",
            confidence=0.62,
            confidence_tier="medium",
        )
        cand_no = await repo.create_task(
            title="Кандидат 2",
            dod="Шум из рассылки",
            status="candidate",
            confidence=0.51,
            confidence_tier="medium",
        )
        await session.commit()

    settings = Settings(
        telegram_bot_token="123456:TESTTOKEN",
        telegram_allowed_user_id=777,
        telegram_widget_enabled=True,
    )
    widget_api = AsyncMock(spec=TelegramWidgetAPI)
    widget_api.send_widget.return_value = {"message_id": 500}
    widget_api.answer_widget_query.return_value = True

    tg_bot = LifeOSTelegramBot(
        settings=settings,
        session_factory=db_session_factory,
        widget_api=widget_api,
        bot=AsyncMock(),
    )

    # Unauthorized user must be rejected without touching DB or widget API.
    denied = await tg_bot.handle_widget_query(
        WidgetQueryUpdate(
            id="q-denied",
            from_user_id=888,
            chat_id=888,
            data=json.dumps({"a": "t", "id": task.id}),
        )
    )
    assert denied is False
    widget_api.answer_widget_query.assert_not_called()

    # Toggle task -> done
    ok_toggle = await tg_bot.handle_widget_query(
        WidgetQueryUpdate(
            id="q-toggle",
            from_user_id=777,
            chat_id=777,
            data=json.dumps({"a": "t", "id": task.id}),
        )
    )
    assert ok_toggle is True

    # Approve candidate -> active
    ok_yes = await tg_bot.handle_widget_query(
        WidgetQueryUpdate(
            id="q-yes",
            from_user_id=777,
            chat_id=777,
            data=json.dumps({"a": "y", "id": cand_yes.id}),
        )
    )
    assert ok_yes is True

    # Dismiss candidate -> dismissed
    ok_no = await tg_bot.handle_widget_query(
        WidgetQueryUpdate(
            id="q-no",
            from_user_id=777,
            chat_id=777,
            data=json.dumps({"a": "n", "id": cand_no.id}),
        )
    )
    assert ok_no is True
    assert widget_api.answer_widget_query.await_count == 3

    async with db_session_factory() as session:
        t_row = await session.get(Task, task.id)
        y_row = await session.get(Task, cand_yes.id)
        n_row = await session.get(Task, cand_no.id)
        assert t_row is not None and t_row.status == "done"
        assert y_row is not None and y_row.status == "active" and y_row.confidence == 1.0
        assert n_row is not None and n_row.status == "dismissed"


async def test_direct_capture_creates_raw_item_and_blocks_stranger(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Direct text/voice/photo capture stores RawItem(confidence=1.0) and enqueues."""
    enqueued: list[str] = []

    async def fake_enqueue(raw_item_id: str) -> None:
        enqueued.append(raw_item_id)

    settings = Settings(
        telegram_bot_token="123456:TESTTOKEN",
        telegram_allowed_user_id=555,
        telegram_widget_enabled=False,
    )
    tg_bot = LifeOSTelegramBot(
        settings=settings,
        session_factory=db_session_factory,
        enqueue_raw_item_fn=fake_enqueue,
        widget_api=AsyncMock(spec=TelegramWidgetAPI),
        bot=AsyncMock(),
    )

    stranger_msg = SimpleNamespace(
        from_user=SimpleNamespace(id=111),
        chat=SimpleNamespace(id=111),
        message_id=1,
        text="Несанкционированное сообщение",
        caption=None,
        voice=None,
        photo=None,
        forward_origin=None,
        forward_from=None,
        forward_from_chat=None,
        answer=AsyncMock(),
    )
    await tg_bot._on_direct_capture(stranger_msg)  # type: ignore[arg-type]
    assert enqueued == []

    voice_msg = SimpleNamespace(
        from_user=SimpleNamespace(id=555),
        chat=SimpleNamespace(id=555),
        message_id=2,
        text=None,
        caption=None,
        voice=SimpleNamespace(file_id="voice-ogg-1", mime_type="audio/ogg"),
        photo=None,
        forward_origin=None,
        forward_from=None,
        forward_from_chat=None,
        answer=AsyncMock(),
    )
    await tg_bot._on_direct_capture(voice_msg)  # type: ignore[arg-type]

    assert len(enqueued) == 1
    async with db_session_factory() as session:
        res = await session.execute(select(RawItem))
        items = list(res.scalars().all())
        assert len(items) == 1
        assert items[0].source == "telegram_bot"
        assert items[0].modality == "voice"
        assert items[0].media_mime_type == "audio/ogg"
        assert items[0].metadata_json["confidence"] == 1.0
