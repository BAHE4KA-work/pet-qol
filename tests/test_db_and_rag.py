"""Tests for LifeOS ORM models, repository operations, and SQLite RAG vector search."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from lifeos.db.models import EmbeddingRecord
from lifeos.db.repositories import LifeOSRepository, compute_sha256
from tests.conftest import FakeRedis


async def test_raw_item_creation_and_sha256_dedup(
    repo: LifeOSRepository,
    db_session: AsyncSession,
) -> None:
    """create_raw_item_if_new deduplicates by both content_hash and source_ref."""
    digest = compute_sha256("Pay electricity bill tomorrow")
    assert digest == compute_sha256(b"Pay electricity bill tomorrow")
    assert len(digest) == 64

    item1, created1 = await repo.create_raw_item_if_new(
        source="gmail",
        source_ref="gmail:msg-100",
        content_hash=digest,
        modality="text",
        raw_text="Pay electricity bill tomorrow",
        metadata_json={"sender": "billing@example.com"},
    )
    await db_session.commit()

    assert created1 is True
    assert item1.source == "gmail"
    assert item1.metadata_json == {"sender": "billing@example.com"}

    fetched = await repo.get_raw_item(item1.id)
    assert fetched is not None and fetched.id == item1.id

    # Duplicate content_hash with a different source_ref returns existing item.
    dup_hash, created_dup_hash = await repo.create_raw_item_if_new(
        source="telegram_userbot",
        source_ref="tg:msg-999",
        content_hash=digest,
        raw_text="Pay electricity bill tomorrow",
    )
    assert created_dup_hash is False
    assert dup_hash.id == item1.id

    # Duplicate source_ref with a different content_hash also returns existing item.
    dup_ref, created_dup_ref = await repo.create_raw_item_if_new(
        source="gmail",
        source_ref="gmail:msg-100",
        content_hash=compute_sha256("Edited body"),
        raw_text="Edited body",
    )
    assert created_dup_ref is False
    assert dup_ref.id == item1.id


async def test_goal_project_task_relations_and_toggle_done(
    repo: LifeOSRepository,
    db_session: AsyncSession,
) -> None:
    """Goal -> Project -> Task/Subtask hierarchy and toggle_task_done persist cleanly."""
    goal = await repo.upsert_goal(
        title="Launch LifeOS v1",
        notion_page_id="notion-goal-1",
        horizon="quarter",
        priority=1,
        progress_pct=25.0,
    )
    updated_goal = await repo.upsert_goal(
        title="Launch LifeOS v1.0",
        notion_page_id="notion-goal-1",
        horizon="quarter",
        priority=1,
        progress_pct=50.0,
    )
    assert updated_goal.id == goal.id
    assert len(await repo.list_active_goals()) == 1

    project = await repo.upsert_project(
        title="Backend Core",
        goal_id=goal.id,
        notion_page_id="notion-proj-1",
        milestone="Wave 2 complete",
    )
    assert project.goal_id == goal.id

    raw_item, _ = await repo.create_raw_item_if_new(
        source="siri",
        source_ref="siri:1",
        content_hash=compute_sha256("Write integration tests"),
        raw_text="Write integration tests",
    )

    today = date(2026, 11, 24)
    yesterday = today - timedelta(days=1)

    parent_task = await repo.create_task(
        title="Ship test suite",
        dod="All pytest tests pass",
        goal_id=goal.id,
        project_id=project.id,
        raw_item_id=raw_item.id,
        duration_bucket=3,
        cognitive_load=4,
        scheduled_date=today,
        day_anchor="morning",
    )
    assert parent_task.effort == pytest.approx(12.0)

    subtask = await repo.create_task(
        title="Write DB tests",
        dod="test_db_and_rag.py green",
        goal_id=goal.id,
        project_id=project.id,
        parent_task_id=parent_task.id,
        duration_bucket=1,
        cognitive_load=2,
        scheduled_date=yesterday,
    )
    candidate = await repo.create_task(
        title="Maybe refactor helper",
        dod="Helper simplified",
        status="candidate",
        confidence=0.62,
        confidence_tier="medium",
    )
    await db_session.commit()

    assert [t.id for t in await repo.list_tasks_for_date(today)] == [parent_task.id]
    assert {t.id for t in await repo.list_active_tasks()} == {parent_task.id, subtask.id}
    assert [t.id for t in await repo.list_candidates()] == [candidate.id]
    assert [t.id for t in await repo.list_unfinished_before(today)] == [subtask.id]

    done_task = await repo.toggle_task_done(parent_task.id)
    assert done_task is not None
    assert done_task.status == "done"
    assert done_task.completed_at is not None

    reopened = await repo.toggle_task_done(parent_task.id)
    assert reopened is not None
    assert reopened.status == "active"
    assert reopened.completed_at is None

    assert await repo.toggle_task_done("missing-task-id") is None


async def test_telemetry_weekly_insight_and_sync_state(
    repo: LifeOSRepository,
    db_session: AsyncSession,
) -> None:
    """DailyTelemetry, WeeklyInsight, and SyncState upsert and query reliably."""
    day1 = date(2026, 11, 22)
    day2 = date(2026, 11, 23)
    day3 = date(2026, 11, 24)

    t1 = await repo.get_or_create_telemetry(day1)
    t1.sleep_hours = 7.5
    t1_again = await repo.get_or_create_telemetry(day1)
    assert t1_again.id == t1.id

    t2 = await repo.get_or_create_telemetry(day2)
    t2.completion_rate = 0.85
    t3 = await repo.get_or_create_telemetry(day3)
    t3.completion_rate = 0.90

    recent = await repo.list_recent_telemetry(days=1, up_to_day=day3)
    assert [rec.day for rec in recent] == [day2, day3]

    week_start = date(2026, 11, 23)
    insight_v1 = await repo.upsert_weekly_insight(
        week_start=week_start,
        summary_markdown="Initial summary",
        sleep_productivity_corr=0.65,
        procrastination_patterns=["Evening fatigue"],
        goal_progress={"Launch": "On track"},
        recommendations=["Sleep before 23:30"],
    )
    insight_v2 = await repo.upsert_weekly_insight(
        week_start=week_start,
        summary_markdown="Updated summary",
        sleep_productivity_corr=0.72,
        recommendations=["Protect morning block"],
    )
    assert insight_v2.id == insight_v1.id
    assert insight_v2.summary_markdown == "Updated summary"
    assert insight_v2.sleep_productivity_corr == pytest.approx(0.72)

    assert await repo.get_sync_state("gmail_history") is None
    await repo.set_sync_state("gmail_history", {"history_id": "12345"})
    await repo.set_sync_state("gmail_history", {"history_id": "12399"})
    await db_session.commit()

    state = await repo.get_sync_state("gmail_history")
    assert state == {"history_id": "12399"}


async def test_embedding_model_guard_and_sqlite_semantic_search(
    repo: LifeOSRepository,
    db_session: AsyncSession,
) -> None:
    """EmbeddingRecord rejects non-gemini-embedding-2 models and ranks cosine similarity."""
    with pytest.raises(ValueError, match="gemini-embedding-2"):
        await repo.upsert_embedding(
            entity_type="task",
            entity_id="t-invalid",
            content_hash=compute_sha256("invalid"),
            embedding=[1.0, 0.0, 0.0, 0.0],
            model_name="text-embedding-004",
        )

    rec_task_1 = await repo.upsert_embedding(
        entity_type="task",
        entity_id="task-1",
        content_hash=compute_sha256("Prepare quarterly tax report"),
        embedding=[0.95, 0.05, 0.0, 0.0],
        text_preview="Prepare quarterly tax report",
    )
    rec_task_2 = await repo.upsert_embedding(
        entity_type="task",
        entity_id="task-2",
        content_hash=compute_sha256("Buy coffee beans"),
        embedding=[0.0, 1.0, 0.0, 0.0],
        text_preview="Buy coffee beans",
    )
    rec_goal = await repo.upsert_embedding(
        entity_type="goal",
        entity_id="goal-1",
        content_hash=compute_sha256("Financial clarity"),
        embedding=[0.8, 0.2, 0.0, 0.0],
        text_preview="Financial clarity",
    )
    # Updating same (entity_type, entity_id) reuses the record row.
    updated_task_1 = await repo.upsert_embedding(
        entity_type="task",
        entity_id="task-1",
        content_hash=compute_sha256("Prepare annual tax report"),
        embedding=[1.0, 0.0, 0.0, 0.0],
        text_preview="Prepare annual tax report",
    )
    await db_session.commit()

    assert isinstance(rec_task_1, EmbeddingRecord)
    assert updated_task_1.id == rec_task_1.id
    assert rec_task_2.entity_id == "task-2"
    assert rec_goal.entity_type == "goal"

    all_matches = await repo.semantic_search([1.0, 0.0, 0.0, 0.0], limit=3)
    assert [rec.entity_id for rec, _ in all_matches] == ["task-1", "goal-1", "task-2"]
    assert all_matches[0][1] == pytest.approx(1.0)
    assert all_matches[1][1] > all_matches[2][1]
    assert all_matches[2][1] == pytest.approx(0.0)

    task_only = await repo.semantic_search(
        [1.0, 0.0, 0.0, 0.0],
        limit=2,
        entity_types=["task"],
    )
    assert [rec.entity_id for rec, _ in task_only] == ["task-1", "task-2"]


async def test_fake_redis_stub(fake_redis: FakeRedis) -> None:
    """FakeRedis supports get, incr, decr, expire, and ttl."""
    assert await fake_redis.get("missing") is None
    assert await fake_redis.ttl("missing") == -2

    assert await fake_redis.incr("counter") == 1
    assert await fake_redis.incr("counter") == 2
    assert await fake_redis.get("counter") == b"2"
    assert await fake_redis.ttl("counter") == -1

    assert await fake_redis.expire("counter", 60) is True
    ttl = await fake_redis.ttl("counter")
    assert 1 <= ttl <= 60

    assert await fake_redis.decr("counter") == 1
    assert await fake_redis.get("counter") == b"1"
