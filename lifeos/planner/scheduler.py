"""Daily anchor assignment, silent rollover, micro-step splitting, and view builder."""

from __future__ import annotations

from datetime import date
from typing import cast

from lifeos.db.models import Task
from lifeos.db.repositories import LifeOSRepository
from lifeos.schemas.ai import ConfidenceTier, DayAnchor, EnergyTier, ItemType, SubtaskSpec
from lifeos.schemas.planner import (
    BudgetBreakdown,
    CalendarSlot,
    CandidateItemDTO,
    DailyPlanView,
    TaskCardDTO,
)

_ANCHOR_BY_ENERGY: dict[str, DayAnchor] = {
    "deep": "morning",
    "medium": "afternoon",
    "low": "evening",
}
_VALID_ANCHORS: set[str] = {"morning", "afternoon", "evening"}


def _resolve_anchor(task: Task) -> DayAnchor:
    if task.day_anchor in _VALID_ANCHORS:
        return cast(DayAnchor, task.day_anchor)
    return _ANCHOR_BY_ENERGY.get(task.energy_tier, "afternoon")


def assign_daily_plan(
    tasks: list[Task],
    day: date,
    budget: BudgetBreakdown,
    calendar_slots: list[CalendarSlot] | None = None,
) -> dict[DayAnchor, list[Task]]:
    """Select active non-event tasks within budget and group them into day anchors."""
    _ = calendar_slots
    plan: dict[DayAnchor, list[Task]] = {
        "morning": [],
        "afternoon": [],
        "evening": [],
    }
    eligible = [
        task for task in tasks if task.status == "active" and task.item_type != "event"
    ]
    eligible.sort(key=lambda t: (-t.urgency, -t.confidence, t.effort))

    used_effort = 0.0
    deep_count = 0

    for task in eligible:
        if budget.strip_deep_tasks and task.energy_tier == "deep" and deep_count >= 1:
            continue
        if used_effort + task.effort > budget.daily_budget:
            continue

        anchor = _resolve_anchor(task)
        task.day_anchor = anchor
        task.scheduled_date = day
        used_effort += task.effort
        if task.energy_tier == "deep":
            deep_count += 1
        plan[anchor].append(task)

    return plan


def silent_rollover(unfinished_tasks: list[Task], today: date) -> list[Task]:
    """Silently reschedule unfinished tasks to today and increment deferral_count."""
    rolled_over: list[Task] = []
    for task in unfinished_tasks:
        if task.status != "active":
            continue
        task.scheduled_date = today
        task.deferral_count += 1
        rolled_over.append(task)
    return rolled_over


def find_tasks_needing_split(tasks: list[Task], threshold: int = 3) -> list[Task]:
    """Return active root tasks whose deferral_count meets or exceeds threshold."""
    return [
        task
        for task in tasks
        if task.status == "active"
        and task.deferral_count >= threshold
        and task.parent_task_id is None
    ]


async def apply_micro_step_split(
    repo: LifeOSRepository,
    parent_task: Task,
    subtasks: list[SubtaskSpec],
    scheduled_date: date,
) -> list[Task]:
    """Persist low-friction child micro-step tasks linked to parent_task."""
    created: list[Task] = []
    anchor = parent_task.day_anchor if parent_task.day_anchor in _VALID_ANCHORS else "evening"
    for spec in subtasks:
        child = await repo.create_task(
            parent_task_id=parent_task.id,
            project_id=parent_task.project_id,
            goal_id=parent_task.goal_id,
            raw_item_id=parent_task.raw_item_id,
            title=spec.title,
            dod=spec.dod,
            item_type="task",
            status="active",
            confidence=parent_task.confidence,
            confidence_tier=parent_task.confidence_tier,
            energy_tier="low",
            duration_bucket=1,
            cognitive_load=spec.cognitive_load,
            effort=float(1 * spec.cognitive_load),
            urgency=parent_task.urgency,
            day_anchor=anchor,
            scheduled_date=scheduled_date,
            has_exact_time=False,
        )
        created.append(child)
    return created


def _to_task_card(task: Task) -> TaskCardDTO:
    return TaskCardDTO(
        id=task.id,
        title=task.title,
        dod=task.dod,
        item_type=cast(ItemType, task.item_type),
        status=task.status,
        confidence=task.confidence,
        confidence_tier=cast(ConfidenceTier, task.confidence_tier),
        energy_tier=cast(EnergyTier, task.energy_tier),
        effort=task.effort,
        day_anchor=cast(DayAnchor | None, task.day_anchor),
        scheduled_date=task.scheduled_date,
        due_at=task.due_at,
        has_exact_time=task.has_exact_time,
        deferral_count=task.deferral_count,
    )


def _to_candidate_dto(task: Task) -> CandidateItemDTO:
    return CandidateItemDTO(
        id=task.id,
        title=task.title,
        dod=task.dod,
        confidence=task.confidence,
        energy_tier=cast(EnergyTier, task.energy_tier),
        effort=task.effort,
        source_link=task.source_link,
    )


def build_daily_plan_view(
    day: date,
    tasks: list[Task],
    candidates: list[Task],
    budget: BudgetBreakdown,
    calendar_slots: list[CalendarSlot] | None = None,
) -> DailyPlanView:
    """Build DailyPlanView DTO for Telegram widgets and API responses."""
    morning: list[TaskCardDTO] = []
    afternoon: list[TaskCardDTO] = []
    evening: list[TaskCardDTO] = []

    day_tasks = [
        task
        for task in tasks
        if task.status in ("active", "done")
        and (task.scheduled_date is None or task.scheduled_date == day)
    ]

    effort_planned = 0.0
    effort_done = 0.0
    for task in day_tasks:
        effort_planned += task.effort
        if task.status == "done":
            effort_done += task.effort
        anchor = _resolve_anchor(task)
        card = _to_task_card(task)
        if anchor == "morning":
            morning.append(card)
        elif anchor == "afternoon":
            afternoon.append(card)
        else:
            evening.append(card)

    candidate_dtos = [_to_candidate_dto(c) for c in candidates if c.status == "candidate"]
    return DailyPlanView(
        day=day,
        budget=budget,
        effort_planned=effort_planned,
        effort_done=effort_done,
        morning=morning,
        afternoon=afternoon,
        evening=evening,
        candidates=candidate_dtos,
        calendar_events=list(calendar_slots or []),
    )
