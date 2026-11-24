"""Tests for confidence routing, Goldilocks controller, scheduler, and weekly service."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from lifeos.config import Settings
from lifeos.db.base import Base, create_engine_and_sessionmaker
from lifeos.db.models import DailyTelemetry, Task, WeeklyInsight
from lifeos.db.repositories import LifeOSRepository
from lifeos.planner.goldilocks import (
    calculate_effort,
    compute_daily_budget,
    compute_k_calendar,
    compute_k_health,
    compute_k_velocity,
)
from lifeos.planner.routing import DIRECT_SOURCES, classify_confidence_tier, route_extracted_item
from lifeos.planner.scheduler import (
    apply_micro_step_split,
    assign_daily_plan,
    build_daily_plan_view,
    find_tasks_needing_split,
    silent_rollover,
)
from lifeos.planner.weekly import WeeklyPlanningService, pearson_correlation
from lifeos.schemas.ai import (
    ExtractedItem,
    SubtaskSpec,
    WeeklyAnalyticsOutput,
    WeeklyPlanOutput,
    WeeklyPlanSlot,
)
from lifeos.schemas.planner import CalendarSlot, HealthSnapshot


def _make_settings() -> Settings:
    return Settings(high_confidence_threshold=0.78, medium_confidence_threshold=0.45)


def _make_task(
    *,
    task_id: str = "t-1",
    title: str = "Sample task",
    item_type: str = "task",
    status: str = "active",
    energy_tier: str = "medium",
    effort: float = 6.0,
    urgency: int = 3,
    confidence: float = 0.9,
    day_anchor: str | None = None,
    scheduled_date: date | None = None,
    has_exact_time: bool = False,
    deferral_count: int = 0,
    parent_task_id: str | None = None,
) -> Task:
    return Task(
        id=task_id,
        title=title,
        dod="Done",
        item_type=item_type,
        status=status,
        confidence=confidence,
        confidence_tier="high",
        energy_tier=energy_tier,
        duration_bucket=2,
        cognitive_load=3,
        effort=effort,
        urgency=urgency,
        day_anchor=day_anchor,
        scheduled_date=scheduled_date,
        has_exact_time=has_exact_time,
        deferral_count=deferral_count,
        parent_task_id=parent_task_id,
    )


def _make_telemetry(
    day: date,
    *,
    effort_planned: float = 10.0,
    effort_done: float = 8.0,
    completion_rate: float = 0.8,
    deferrals_count: int = 0,
    sleep_hours: float | None = None,
) -> DailyTelemetry:
    return DailyTelemetry(
        day=day,
        effort_planned=effort_planned,
        effort_done=effort_done,
        completion_rate=completion_rate,
        deferrals_count=deferrals_count,
        sleep_hours=sleep_hours,
    )


def test_confidence_routing_tiers_and_direct_override() -> None:
    cfg = _make_settings()
    assert DIRECT_SOURCES == {"siri", "telegram_bot", "notion"}

    assert classify_confidence_tier(0.78, "gmail", cfg) == "high"
    assert classify_confidence_tier(0.45, "gmail", cfg) == "medium"
    assert classify_confidence_tier(0.44, "gmail", cfg) == "low"

    for direct_source in DIRECT_SOURCES:
        assert classify_confidence_tier(0.10, direct_source, cfg) == "high"

    high_task = route_extracted_item(
        ExtractedItem(title="Pay rent", dod="Transfer sent", item_type="task"),
        confidence=0.85,
        source="gmail",
        settings=cfg,
    )
    assert high_task == {
        "status": "active",
        "confidence_tier": "high",
        "push_to_reminders": True,
        "push_to_calendar": False,
        "show_in_widget_candidates": False,
    }

    high_event = route_extracted_item(
        ExtractedItem(title="Dentist", dod="Visit completed", item_type="event"),
        confidence=0.20,
        source="siri",
        settings=cfg,
    )
    assert high_event == {
        "status": "active",
        "confidence_tier": "high",
        "push_to_reminders": False,
        "push_to_calendar": True,
        "show_in_widget_candidates": False,
    }

    candidate = route_extracted_item(
        ExtractedItem(title="Read paper", dod="Summary written", item_type="task"),
        confidence=0.60,
        source="telegram_userbot",
        settings=cfg,
    )
    assert candidate == {
        "status": "candidate",
        "confidence_tier": "medium",
        "push_to_reminders": False,
        "push_to_calendar": False,
        "show_in_widget_candidates": True,
    }

    someday = route_extracted_item(
        ExtractedItem(title="Learn piano", dod="First lesson", item_type="goal_idea"),
        confidence=0.20,
        source="apple_notes",
        settings=cfg,
    )
    assert someday == {
        "status": "someday",
        "confidence_tier": "low",
        "push_to_reminders": False,
        "push_to_calendar": False,
        "show_in_widget_candidates": False,
    }


def test_goldilocks_math_and_velocity_corridor() -> None:
    assert calculate_effort(3, 4) == 12.0

    assert compute_k_calendar(0.0) == pytest.approx(1.0)
    assert compute_k_calendar(10.0) == pytest.approx(0.30)
    assert compute_k_calendar(4.25) == pytest.approx(0.50)

    assert compute_k_health(None) == (1.0, False)
    assert compute_k_health(HealthSnapshot(sleep_hours=5.5, hrv_ms=60.0)) == (0.75, True)
    assert compute_k_health(HealthSnapshot(sleep_hours=8.0, hrv_ms=25.0)) == (0.75, True)
    assert compute_k_health(HealthSnapshot(sleep_hours=6.5, hrv_ms=50.0)) == (0.85, False)
    assert compute_k_health(HealthSnapshot(sleep_hours=8.0, hrv_ms=65.0)) == (1.10, False)
    assert compute_k_health(HealthSnapshot(sleep_hours=7.0, hrv_ms=45.0)) == (1.0, False)

    base_day = date(2026, 11, 10)
    # Empty history preserves current_k
    assert compute_k_velocity([], current_k=1.05) == (1.05, None)

    # Inside 75-85% corridor holds current_k
    corridor_rows = [
        _make_telemetry(base_day + timedelta(days=i), completion_rate=0.80) for i in range(5)
    ]
    k_hold, avg_hold = compute_k_velocity(corridor_rows, current_k=1.0)
    assert k_hold == pytest.approx(1.0)
    assert avg_hold == pytest.approx(0.80)

    # Low completion (< 0.65) reduces by 10%
    low_rows = [
        _make_telemetry(base_day + timedelta(days=i), completion_rate=0.50) for i in range(4)
    ]
    k_down, _ = compute_k_velocity(low_rows, current_k=1.0)
    assert k_down == pytest.approx(0.90)

    # Rising deferrals over last 3 days reduces by 10% even if avg rate is in corridor
    rising_deferrals = [
        _make_telemetry(base_day, completion_rate=0.80, deferrals_count=0),
        _make_telemetry(base_day + timedelta(days=1), completion_rate=0.80, deferrals_count=1),
        _make_telemetry(base_day + timedelta(days=2), completion_rate=0.80, deferrals_count=3),
    ]
    k_def, _ = compute_k_velocity(rising_deferrals, current_k=1.0)
    assert k_def == pytest.approx(0.90)

    # Last 3 days > 0.90 raises by 10%
    high_rows = [
        _make_telemetry(base_day + timedelta(days=i), completion_rate=0.95) for i in range(3)
    ]
    k_up, _ = compute_k_velocity(high_rows, current_k=1.0)
    assert k_up == pytest.approx(1.10)

    # Slight nudges and clamps [0.65, 1.35]
    nudge_down_rows = [_make_telemetry(base_day, completion_rate=0.70)]
    assert compute_k_velocity(nudge_down_rows, current_k=1.0)[0] == pytest.approx(0.95)
    assert compute_k_velocity(nudge_down_rows, current_k=0.65)[0] == pytest.approx(0.65)

    nudge_up_rows = [_make_telemetry(base_day, completion_rate=0.88)]
    assert compute_k_velocity(nudge_up_rows, current_k=1.0)[0] == pytest.approx(1.05)
    assert compute_k_velocity(high_rows, current_k=1.30)[0] == pytest.approx(1.35)

    budget = compute_daily_budget(
        base_capacity=20.0,
        busy_hours=0.0,
        health=HealthSnapshot(sleep_hours=5.2, hrv_ms=40.0),
        recent_telemetry=corridor_rows,
        current_k_velocity=1.0,
        settings=_make_settings(),
    )
    assert budget.k_calendar == pytest.approx(1.0)
    assert budget.k_health == pytest.approx(0.75)
    assert budget.k_velocity == pytest.approx(1.0)
    assert budget.strip_deep_tasks is True
    assert budget.daily_budget == pytest.approx(15.0)


def test_scheduler_assign_strip_deep_and_rollover() -> None:
    day = date(2026, 11, 24)
    budget = compute_daily_budget(
        base_capacity=20.0,
        busy_hours=0.0,
        health=HealthSnapshot(sleep_hours=5.5),
        recent_telemetry=[],
        settings=_make_settings(),
    )
    assert budget.strip_deep_tasks is True
    assert budget.daily_budget == pytest.approx(15.0)

    tasks = [
        _make_task(task_id="d1", energy_tier="deep", effort=6.0, urgency=5),
        _make_task(task_id="d2", energy_tier="deep", effort=6.0, urgency=4),
        _make_task(task_id="m1", energy_tier="medium", effort=5.0, urgency=4),
        _make_task(
            task_id="l1",
            energy_tier="low",
            effort=4.0,
            urgency=2,
            day_anchor="morning",
            has_exact_time=False,
        ),
        _make_task(task_id="ev1", item_type="event", energy_tier="medium", effort=3.0, urgency=5),
        _make_task(task_id="cand", status="candidate", energy_tier="low", effort=2.0, urgency=5),
    ]

    plan = assign_daily_plan(tasks, day=day, budget=budget)
    assigned_ids = [t.id for bucket in plan.values() for t in bucket]

    assert "d1" in assigned_ids
    assert "d2" not in assigned_ids  # stripped second deep task
    assert "m1" in assigned_ids
    assert "l1" in assigned_ids
    assert "ev1" not in assigned_ids
    assert "cand" not in assigned_ids
    assert [t.id for t in plan["morning"]] == ["d1", "l1"]  # l1 preserved explicit anchor
    assert [t.id for t in plan["afternoon"]] == ["m1"]
    assert all(not t.has_exact_time for bucket in plan.values() for t in bucket)

    rolled = silent_rollover([tasks[0], tasks[2]], today=day + timedelta(days=1))
    assert len(rolled) == 2
    assert tasks[0].scheduled_date == day + timedelta(days=1)
    assert tasks[0].deferral_count == 1

    needing_split = find_tasks_needing_split(
        [
            _make_task(task_id="ok", deferral_count=2),
            _make_task(task_id="stuck", deferral_count=3),
            _make_task(task_id="child", deferral_count=4, parent_task_id="stuck"),
        ],
        threshold=3,
    )
    assert [t.id for t in needing_split] == ["stuck"]

    view = build_daily_plan_view(
        day=day,
        tasks=[
            _make_task(
                task_id="a",
                status="done",
                effort=4.0,
                day_anchor="morning",
                scheduled_date=day,
            ),
            _make_task(
                task_id="b",
                status="active",
                effort=6.0,
                day_anchor="afternoon",
                scheduled_date=day,
            ),
        ],
        candidates=[_make_task(task_id="c1", status="candidate", effort=3.0)],
        budget=budget,
        calendar_slots=[
            CalendarSlot(
                uid="u1",
                title="Sync",
                start_at=datetime(2026, 11, 24, 10, 0, tzinfo=UTC),
                end_at=datetime(2026, 11, 24, 11, 0, tzinfo=UTC),
            )
        ],
    )
    assert view.effort_planned == pytest.approx(10.0)
    assert view.effort_done == pytest.approx(4.0)
    assert len(view.morning) == 1
    assert len(view.afternoon) == 1
    assert len(view.candidates) == 1
    assert len(view.calendar_events) == 1


@pytest.mark.asyncio
async def test_apply_micro_step_split_persists_child_tasks() -> None:
    engine, session_factory = create_engine_and_sessionmaker("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        async with session_factory() as session:
            repo = LifeOSRepository(session)
            parent = await repo.create_task(
                title="Write architecture RFC",
                dod="RFC merged",
                energy_tier="deep",
                duration_bucket=4,
                cognitive_load=5,
                deferral_count=3,
            )
            subtasks = [
                SubtaskSpec(title="Create RFC outline", dod="Headings drafted", cognitive_load=2),
                SubtaskSpec(
                    title="Write problem statement",
                    dod="Section 1 done",
                    cognitive_load=2,
                ),
            ]
            target_day = date(2026, 11, 24)
            children = await apply_micro_step_split(repo, parent, subtasks, target_day)
            await session.commit()

            assert len(children) == 2
            for child in children:
                assert child.parent_task_id == parent.id
                assert child.energy_tier == "low"
                assert child.duration_bucket == 1
                assert child.status == "active"
                assert child.scheduled_date == target_day
    finally:
        await engine.dispose()


class _FakeTier3Agent:
    def __init__(self, target_task_id: str, week_start: date) -> None:
        self.target_task_id = target_task_id
        self.week_start = week_start
        self.received_corr: float | None = None

    async def generate_weekly_plan(
        self,
        *,
        week_start: date,
        tasks: list[dict[str, Any]],
        goals: list[dict[str, Any]],
        calendar_slots: list[dict[str, Any]],
    ) -> WeeklyPlanOutput:
        _ = (tasks, goals, calendar_slots)
        return WeeklyPlanOutput(
            week_start=week_start,
            focus_theme="Deep shipping",
            slots=[
                WeeklyPlanSlot(
                    task_id=self.target_task_id,
                    scheduled_date=week_start + timedelta(days=1),
                    day_anchor="morning",
                    rationale="High urgency",
                )
            ],
        )

    async def generate_weekly_analytics(
        self,
        *,
        week_start: date,
        telemetry: list[dict[str, Any]],
        tasks: list[dict[str, Any]],
        goals: list[dict[str, Any]],
        sleep_productivity_corr: float | None,
    ) -> WeeklyAnalyticsOutput:
        _ = (telemetry, tasks, goals)
        self.received_corr = sleep_productivity_corr
        return WeeklyAnalyticsOutput(
            week_start=week_start,
            summary_markdown="Strong sleep-output link.",
            sleep_productivity_corr=None,
            procrastination_patterns=["Late afternoon drift"],
            goal_progress_notes={"g1": "On track"},
            recommendations=["Protect 8h sleep"],
        )


def test_pearson_correlation_cases() -> None:
    assert pearson_correlation([1.0], [2.0]) is None
    assert pearson_correlation([1.0, 2.0], [1.0]) is None
    assert pearson_correlation([7.0, 7.0, 7.0], [0.5, 0.7, 0.9]) is None
    assert pearson_correlation([6.0, 7.0, 8.0], [0.5, 0.7, 0.9]) == pytest.approx(1.0)
    assert pearson_correlation([6.0, 7.0, 8.0], [0.9, 0.7, 0.5]) == pytest.approx(-1.0)


@pytest.mark.asyncio
async def test_weekly_service_schedules_and_persists_insight() -> None:
    engine, session_factory = create_engine_and_sessionmaker("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    week_start = date(2026, 11, 23)
    try:
        async with session_factory() as session:
            repo = LifeOSRepository(session)
            task = await repo.create_task(
                title="Ship weekly service",
                dod="Tests green",
                energy_tier="deep",
                duration_bucket=3,
                cognitive_load=4,
            )
            for idx, (sleep_h, rate) in enumerate([(6.0, 0.5), (7.0, 0.7), (8.0, 0.9)]):
                rec = await repo.get_or_create_telemetry(week_start + timedelta(days=idx))
                rec.sleep_hours = sleep_h
                rec.effort_planned = 10.0
                rec.effort_done = 10.0 * rate
                rec.completion_rate = rate

            agent = _FakeTier3Agent(target_task_id=task.id, week_start=week_start)
            service = WeeklyPlanningService(repo=repo, tier3_agent=agent)
            plan, analytics = await service.run_weekly_cycle(week_start=week_start)
            await session.commit()

            assert len(plan.slots) == 1
            assert agent.received_corr == pytest.approx(1.0)
            assert analytics.sleep_productivity_corr == pytest.approx(1.0)

            updated_task = await repo.get_task(task.id)
            assert updated_task is not None
            assert updated_task.scheduled_date == week_start + timedelta(days=1)
            assert updated_task.day_anchor == "morning"

            res = await session.execute(
                select(WeeklyInsight).where(WeeklyInsight.week_start == week_start)
            )
            insight = res.scalar_one_or_none()
            assert insight is not None
            assert insight.sleep_productivity_corr == pytest.approx(1.0)
            assert insight.summary_markdown == "Strong sleep-output link."
            assert insight.recommendations_json == ["Protect 8h sleep"]
    finally:
        await engine.dispose()
