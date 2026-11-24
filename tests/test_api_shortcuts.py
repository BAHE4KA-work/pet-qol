"""Tests for FastAPI app, Bearer auth, iOS Shortcuts/Siri endpoints, and planner routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lifeos.api import create_app
from lifeos.api.routes_shortcuts import extract_lifeos_tag
from lifeos.config import Settings
from lifeos.db.base import Base, create_engine_and_sessionmaker
from lifeos.db.models import DailyTelemetry, RawItem, Task
from lifeos.db.repositories import LifeOSRepository
from lifeos.schemas.ai import ExtractedItem, TriageResult


@pytest.fixture
async def db_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Provide an isolated in-memory async SQLite session factory."""
    engine, session_factory = create_engine_and_sessionmaker("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield session_factory
    await engine.dispose()


def _make_settings(token: str = "super-secret-token") -> Settings:
    return Settings(
        api_bearer_token=token,
        timezone="Europe/Moscow",
        base_daily_capacity=20.0,
    )


def _today_msk() -> date:
    return datetime.now(ZoneInfo("Europe/Moscow")).date()


async def test_healthz_and_readyz(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify /healthz returns 200 and /readyz checks DB connectivity."""
    settings = _make_settings()
    app_ok = create_app(settings=settings, session_factory=db_session_factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_ok),
        base_url="http://test",
    ) as client:
        r_health = await client.get("/healthz")
        assert r_health.status_code == 200
        assert r_health.json() == {"status": "ok"}

        r_ready = await client.get("/readyz")
        assert r_ready.status_code == 200
        assert r_ready.json() == {"status": "ready"}

    app_no_db = create_app(settings=settings, session_factory=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_no_db),
        base_url="http://test",
    ) as client:
        r_fail = await client.get("/readyz")
        assert r_fail.status_code == 503


async def test_bearer_auth_enforcement(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """All protected endpoints reject missing/invalid tokens with 401 and accept valid ones."""
    settings = _make_settings("correct-token-123")
    app = create_app(settings=settings, session_factory=db_session_factory)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        assert (await client.get("/api/v1/plan/today")).status_code == 401
        assert (
            await client.get(
                "/api/v1/plan/today",
                headers={"Authorization": "Bearer wrong-token"},
            )
        ).status_code == 401
        assert (
            await client.get(
                "/api/v1/plan/today",
                headers={"Authorization": "Basic dXNlcjpwYXNz"},
            )
        ).status_code == 401
        assert (
            await client.post("/api/v1/shortcuts/sync", json={})
        ).status_code == 401
        assert (
            await client.post("/api/v1/shortcuts/siri", json={"text": "Buy milk"})
        ).status_code == 401

        ok = await client.get(
            "/api/v1/plan/today",
            headers={"Authorization": "Bearer correct-token-123"},
        )
        assert ok.status_code == 200


def test_extract_lifeos_tag() -> None:
    """extract_lifeos_tag parses `[lifeos:<id>]` from reminder notes."""
    assert extract_lifeos_tag(None) is None
    assert extract_lifeos_tag("No tag here") is None
    assert extract_lifeos_tag("DoD line\n\n[lifeos:abc-123-def]") == "abc-123-def"


async def test_shortcuts_sync_full_roundtrip(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify reminder completion sync, Notes dedup, Health budget calc, and diff filtering."""
    today = _today_msk()
    tomorrow = today + timedelta(days=1)
    next_week = today + timedelta(days=7)
    exact_due = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 14, 30, tzinfo=UTC)

    async with db_session_factory() as session:
        repo = LifeOSRepository(session)
        # Tasks that will be completed from Apple Reminders via 3 matching strategies:
        t_by_id = await repo.create_task(
            title="Finish report",
            dod="PDF exported",
            status="active",
            item_type="task",
            scheduled_date=today,
        )
        t_by_tag = await repo.create_task(
            title="Call dentist",
            dod="Appointment booked",
            status="active",
            item_type="task",
            scheduled_date=today,
        )
        t_by_apple_id = await repo.create_task(
            title="Pay internet",
            dod="Receipt saved",
            status="active",
            item_type="task",
            apple_reminder_id="rem-apple-99",
            scheduled_date=today,
        )
        # Active tasks that MUST appear in reminders_diff:
        t_today = await repo.create_task(
            title="Write specs",
            dod="Spec PR opened",
            status="active",
            item_type="task",
            day_anchor="morning",
            scheduled_date=today,
            due_at=exact_due,
            has_exact_time=False,
        )
        t_tomorrow_exact = await repo.create_task(
            title="Submit taxes",
            dod="Confirmation number saved",
            status="active",
            item_type="task",
            day_anchor="afternoon",
            scheduled_date=tomorrow,
            due_at=exact_due,
            has_exact_time=True,
        )
        # Items that MUST NEVER appear in reminders_diff:
        t_event = await repo.create_task(
            title="Team Sync",
            dod="Meeting attended",
            status="active",
            item_type="event",
            scheduled_date=today,
            due_at=exact_due,
            has_exact_time=True,
        )
        t_cand = await repo.create_task(
            title="Maybe read book",
            dod="Chapter 1",
            status="candidate",
            item_type="task",
            scheduled_date=today,
        )
        t_someday = await repo.create_task(
            title="Learn Japanese",
            dod="Hiragana learned",
            status="someday",
            item_type="goal_idea",
            scheduled_date=today,
        )
        t_future = await repo.create_task(
            title="Next week task",
            dod="Done next week",
            status="active",
            item_type="task",
            scheduled_date=next_week,
        )
        await session.commit()

    enqueued: list[str] = []

    async def fake_enqueue(raw_item_id: str) -> None:
        enqueued.append(raw_item_id)

    settings = _make_settings("sync-token")
    app = create_app(
        settings=settings,
        session_factory=db_session_factory,
        enqueue_fn=fake_enqueue,
    )
    headers = {"Authorization": "Bearer sync-token"}
    completed_ts = datetime(today.year, today.month, today.day, 12, 0, tzinfo=UTC)

    payload: dict[str, Any] = {
        "focus_mode": "Work",
        "reminders": [
            {
                "apple_reminder_id": "apple-1",
                "lifeos_task_id": t_by_id.id,
                "title": "Finish report",
                "is_completed": True,
                "completed_at": completed_ts.isoformat(),
            },
            {
                "apple_reminder_id": "apple-2",
                "title": "Call dentist",
                "notes": f"Appointment booked\n\n[lifeos:{t_by_tag.id}]",
                "is_completed": True,
            },
            {
                "apple_reminder_id": "rem-apple-99",
                "title": "Pay internet",
                "is_completed": True,
            },
        ],
        "notes": [
            {
                "note_id": "n-1",
                "title": "Project Idea",
                "body": "Build an automated weekly review digest.",
            },
            {
                "note_id": "n-2",
                "title": "Project Idea",
                "body": "Build an automated weekly review digest.",
            },
        ],
        "health": {
            "sleep_hours": 5.5,
            "active_energy_kcal": 420.0,
            "hrv_ms": 48.0,
            "resting_hr": 61.0,
        },
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        res1 = await client.post("/api/v1/shortcuts/sync", json=payload, headers=headers)
        assert res1.status_code == 200
        data1 = res1.json()

        assert data1["processed_reminders"] == 3
        assert data1["ingested_notes"] == 1
        assert data1["health_updated"] is True
        # sleep_hours=5.5 -> k_health=0.75 -> 20.0 * 1.0 * 0.75 * 1.0 = 15.0
        assert data1["daily_budget"] == pytest.approx(15.0)
        assert len(enqueued) == 1

        diff_by_id = {item["task_id"]: item for item in data1["reminders_diff"]}
        assert set(diff_by_id.keys()) == {t_today.id, t_tomorrow_exact.id}
        assert t_event.id not in diff_by_id
        assert t_cand.id not in diff_by_id
        assert t_someday.id not in diff_by_id
        assert t_future.id not in diff_by_id

        today_diff = diff_by_id[t_today.id]
        assert today_diff["action"] == "upsert"
        assert today_diff["lifeos_tag"] == f"[lifeos:{t_today.id}]"
        assert today_diff["notes"] == f"Spec PR opened\n\n[lifeos:{t_today.id}]"
        assert today_diff["day_anchor"] == "morning"
        assert today_diff["has_exact_time"] is False
        assert today_diff["due_at"] is None

        tom_diff = diff_by_id[t_tomorrow_exact.id]
        assert tom_diff["has_exact_time"] is True
        assert tom_diff["due_at"] is not None

        # Second sync with same notes must deduplicate (0 new notes, enqueue not called again).
        res2 = await client.post("/api/v1/shortcuts/sync", json=payload, headers=headers)
        assert res2.status_code == 200
        assert res2.json()["ingested_notes"] == 0
        assert len(enqueued) == 1

    async with db_session_factory() as session:
        r1 = await session.get(Task, t_by_id.id)
        r2 = await session.get(Task, t_by_tag.id)
        r3 = await session.get(Task, t_by_apple_id.id)
        assert r1 is not None and r1.status == "done" and r1.apple_reminder_id == "apple-1"
        assert r2 is not None and r2.status == "done" and r2.apple_reminder_id == "apple-2"
        assert r3 is not None and r3.status == "done" and r3.completed_at is not None

        tel_res = await session.execute(
            select(DailyTelemetry).where(DailyTelemetry.day == today)
        )
        tel = tel_res.scalar_one()
        assert tel.sleep_hours == pytest.approx(5.5)
        assert tel.active_energy_kcal == pytest.approx(420.0)
        assert tel.hrv_ms == pytest.approx(48.0)
        assert tel.resting_hr == pytest.approx(61.0)
        assert tel.k_health == pytest.approx(0.75)
        assert tel.daily_budget == pytest.approx(15.0)

        raw_res = await session.execute(select(RawItem))
        raws = list(raw_res.scalars().all())
        assert len(raws) == 1
        assert raws[0].source == "apple_notes"
        assert raws[0].source_ref == "note:n-1"


async def test_siri_capture_with_and_without_ai_router(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify /api/v1/shortcuts/siri creates RawItem + high-confidence active Task."""
    enqueued: list[str] = []

    def sync_enqueue(raw_item_id: str) -> None:
        enqueued.append(raw_item_id)

    settings = _make_settings("siri-token")
    app = create_app(
        settings=settings,
        session_factory=db_session_factory,
        enqueue_fn=sync_enqueue,
    )
    headers = {"Authorization": "Bearer siri-token"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        res = await client.post(
            "/api/v1/shortcuts/siri",
            json={"text": "Buy coffee beans before Friday"},
            headers=headers,
        )
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "active"
        assert body["title"] == "Buy coffee beans before Friday"
        assert body["confidence"] == pytest.approx(1.0)
        assert body["task_id"] is not None
        assert len(enqueued) == 1

    # Now test immediate processing when app.state.ai_router is provided.
    class _FakeAIRouter:
        async def process_raw_item(
            self,
            raw_item: RawItem,
        ) -> tuple[TriageResult, ExtractedItem]:
            _ = raw_item
            return (
                TriageResult(
                    is_actionable=1.0,
                    confidence=1.0,
                    item_type="task",
                    energy_tier="low",
                    needs_hydrogen=0.0,
                    urgency=4,
                    cognitive_load=2,
                ),
                ExtractedItem(
                    title="Order espresso beans",
                    dod="Order placed online",
                    item_type="task",
                    energy_tier="low",
                    duration_bucket=1,
                    cognitive_load=2,
                    urgency=4,
                    suggested_anchor="evening",
                ),
            )

    app_ai = create_app(
        settings=settings,
        session_factory=db_session_factory,
        ai_router=_FakeAIRouter(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_ai),
        base_url="http://test",
    ) as client:
        res_ai = await client.post(
            "/api/v1/shortcuts/siri",
            json={"text": "Remind me to get espresso beans"},
            headers=headers,
        )
        assert res_ai.status_code == 200
        body_ai = res_ai.json()
        assert body_ai["title"] == "Order espresso beans"
        assert body_ai["status"] == "active"
        assert body_ai["confidence"] == pytest.approx(1.0)


async def test_planner_today_toggle_and_candidate_decide(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verify /api/v1/plan/today, task toggle, and candidate approval/dismissal."""
    today = _today_msk()
    async with db_session_factory() as session:
        repo = LifeOSRepository(session)
        task = await repo.create_task(
            title="Review PR",
            dod="Comments posted",
            status="active",
            day_anchor="morning",
            scheduled_date=today,
            effort=4.0,
        )
        cand_yes = await repo.create_task(
            title="Call accountant",
            dod="Invoice confirmed",
            status="candidate",
            confidence=0.60,
            confidence_tier="medium",
            day_anchor="afternoon",
            effort=3.0,
        )
        cand_no = await repo.create_task(
            title="Promo webinar",
            dod="Watch replay",
            status="candidate",
            confidence=0.50,
            confidence_tier="medium",
            effort=2.0,
        )
        await session.commit()

    settings = _make_settings("planner-token")
    app = create_app(settings=settings, session_factory=db_session_factory)
    headers = {"Authorization": "Bearer planner-token"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        plan_res = await client.get("/api/v1/plan/today", headers=headers)
        assert plan_res.status_code == 200
        plan = plan_res.json()
        assert plan["day"] == today.isoformat()
        assert plan["effort_planned"] == pytest.approx(4.0)
        assert plan["effort_done"] == pytest.approx(0.0)
        assert [t["id"] for t in plan["morning"]] == [task.id]
        assert {c["id"] for c in plan["candidates"]} == {cand_yes.id, cand_no.id}

        # Toggle task -> done
        tog1 = await client.post(f"/api/v1/tasks/{task.id}/toggle", headers=headers)
        assert tog1.status_code == 200
        assert tog1.json()["status"] == "done"

        # Toggle task -> active
        tog2 = await client.post(f"/api/v1/tasks/{task.id}/toggle", headers=headers)
        assert tog2.status_code == 200
        assert tog2.json()["status"] == "active"

        # Missing task -> 404
        assert (
            await client.post("/api/v1/tasks/nonexistent/toggle", headers=headers)
        ).status_code == 404

        # Approve candidate -> active and scheduled today
        dec_yes = await client.post(
            f"/api/v1/candidates/{cand_yes.id}/decide",
            json={"approve": True},
            headers=headers,
        )
        assert dec_yes.status_code == 200
        assert dec_yes.json() == {"task_id": cand_yes.id, "status": "active", "approved": True}

        # Dismiss candidate -> dismissed
        dec_no = await client.post(
            f"/api/v1/candidates/{cand_no.id}/decide",
            json={"approve": False},
            headers=headers,
        )
        assert dec_no.status_code == 200
        assert dec_no.json() == {"task_id": cand_no.id, "status": "dismissed", "approved": False}

        # Missing candidate -> 404
        assert (
            await client.post(
                "/api/v1/candidates/nonexistent/decide",
                json={"approve": True},
                headers=headers,
            )
        ).status_code == 404

        # Updated plan reflects approved candidate in afternoon and 0 candidates left
        plan_after = (await client.get("/api/v1/plan/today", headers=headers)).json()
        assert [t["id"] for t in plan_after["afternoon"]] == [cand_yes.id]
        assert plan_after["candidates"] == []
