"""Weekly planning orchestration and passive productivity analytics."""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from lifeos.db.models import DailyTelemetry
from lifeos.db.repositories import LifeOSRepository
from lifeos.logging import get_logger
from lifeos.schemas.ai import WeeklyAnalyticsOutput, WeeklyPlanOutput

logger = get_logger(__name__)


def pearson_correlation(xs: list[float], ys: list[float]) -> float | None:
    """Compute Pearson correlation coefficient r in [-1.0, 1.0], or None if undefined."""
    if len(xs) != len(ys) or len(xs) < 2:
        return None
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    var_x = sum(val * val for val in dx)
    var_y = sum(val * val for val in dy)
    if var_x <= 0.0 or var_y <= 0.0:
        return None
    numerator = sum(a * b for a, b in zip(dx, dy, strict=True))
    corr = numerator / math.sqrt(var_x * var_y)
    return max(-1.0, min(1.0, corr))


def _telemetry_completion_rate(row: DailyTelemetry) -> float:
    if row.completion_rate == 0.0 and row.effort_done > 0.0 and row.effort_planned > 0.0:
        return row.effort_done / row.effort_planned
    return float(row.completion_rate)


class WeeklyPlanningService:
    """Coordinates Tier 3 weekly scheduling, correlation analysis, and insight storage."""

    def __init__(self, repo: LifeOSRepository, tier3_agent: Any) -> None:
        self.repo = repo
        self.tier3_agent = tier3_agent

    async def run_weekly_cycle(
        self,
        week_start: date,
        calendar_slots: list[dict[str, Any]] | None = None,
    ) -> tuple[WeeklyPlanOutput, WeeklyAnalyticsOutput]:
        """Run weekly planning and passive analytics cycle, persisting schedule & insights."""
        tasks = await self.repo.list_active_tasks()
        goals = await self.repo.list_active_goals()
        telemetry = await self.repo.list_recent_telemetry(
            days=14,
            up_to_day=week_start + timedelta(days=6),
        )

        corr_rows = [
            row
            for row in telemetry
            if row.sleep_hours is not None and row.effort_planned > 0
        ]
        sleep_corr = pearson_correlation(
            [float(row.sleep_hours) for row in corr_rows if row.sleep_hours is not None],
            [_telemetry_completion_rate(row) for row in corr_rows],
        )

        task_payloads = [
            {
                "id": task.id,
                "title": task.title,
                "dod": task.dod,
                "energy_tier": task.energy_tier,
                "effort": task.effort,
                "urgency": task.urgency,
                "deferral_count": task.deferral_count,
                "goal_id": task.goal_id,
            }
            for task in tasks
        ]
        goal_payloads = [
            {
                "id": goal.id,
                "title": goal.title,
                "horizon": goal.horizon,
                "priority": goal.priority,
                "progress_pct": goal.progress_pct,
            }
            for goal in goals
        ]
        telemetry_payloads = [
            {
                "day": row.day.isoformat(),
                "sleep_hours": row.sleep_hours,
                "hrv_ms": row.hrv_ms,
                "effort_planned": row.effort_planned,
                "effort_done": row.effort_done,
                "completion_rate": _telemetry_completion_rate(row),
                "deferrals_count": row.deferrals_count,
            }
            for row in telemetry
        ]

        raw_plan = await self.tier3_agent.generate_weekly_plan(
            week_start=week_start,
            tasks=task_payloads,
            goals=goal_payloads,
            calendar_slots=calendar_slots or [],
        )
        plan = (
            raw_plan
            if isinstance(raw_plan, WeeklyPlanOutput)
            else WeeklyPlanOutput.model_validate(raw_plan)
        )

        for slot in plan.slots:
            task = await self.repo.get_task(slot.task_id)
            if task is not None:
                task.scheduled_date = slot.scheduled_date
                task.day_anchor = slot.day_anchor
        await self.repo.session.flush()

        raw_analytics = await self.tier3_agent.generate_weekly_analytics(
            week_start=week_start,
            telemetry=telemetry_payloads,
            tasks=task_payloads,
            goals=goal_payloads,
            sleep_productivity_corr=sleep_corr,
        )
        analytics = (
            raw_analytics
            if isinstance(raw_analytics, WeeklyAnalyticsOutput)
            else WeeklyAnalyticsOutput.model_validate(raw_analytics)
        )
        if sleep_corr is not None:
            analytics.sleep_productivity_corr = sleep_corr

        await self.repo.upsert_weekly_insight(
            week_start=week_start,
            summary_markdown=analytics.summary_markdown,
            sleep_productivity_corr=analytics.sleep_productivity_corr,
            procrastination_patterns=analytics.procrastination_patterns,
            goal_progress=analytics.goal_progress_notes,
            recommendations=analytics.recommendations,
        )
        logger.info(
            "Weekly cycle complete for %s: %d slots scheduled, corr=%s",
            week_start.isoformat(),
            len(plan.slots),
            analytics.sleep_productivity_corr,
        )
        return plan, analytics
