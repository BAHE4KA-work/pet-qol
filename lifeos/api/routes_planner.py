"""Planner API endpoints for Today view, task toggling, and candidate triage."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from lifeos.api.auth import get_db_session, get_today_for_settings, require_bearer_token
from lifeos.config import Settings, get_settings
from lifeos.db.repositories import LifeOSRepository
from lifeos.planner.goldilocks import compute_daily_budget
from lifeos.planner.scheduler import build_daily_plan_view
from lifeos.schemas.planner import DailyPlanView, HealthSnapshot

router = APIRouter(prefix="/api/v1", tags=["planner"])


class CandidateDecisionRequest(BaseModel):
    """Approve or dismiss payload for a medium-confidence candidate."""

    approve: bool


class TaskToggleResponse(BaseModel):
    """Result of toggling a task's completion status."""

    task_id: str
    status: str
    completed_at: datetime | None = None


class CandidateDecisionResponse(BaseModel):
    """Result of approving or dismissing a candidate task."""

    task_id: str
    status: str
    approved: bool


@router.get("/plan/today", response_model=DailyPlanView)
async def get_today_plan(
    _token: str = Depends(require_bearer_token),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> DailyPlanView:
    """Build and return today's Goldilocks DailyPlanView."""
    repo = LifeOSRepository(session)
    today = get_today_for_settings(settings)

    tasks = await repo.list_tasks_for_date(today)
    candidates = await repo.list_candidates()
    telemetry = await repo.get_or_create_telemetry(today)
    recent_telemetry = await repo.list_recent_telemetry(days=14, up_to_day=today)

    health = HealthSnapshot(
        sleep_hours=telemetry.sleep_hours,
        active_energy_kcal=telemetry.active_energy_kcal,
        hrv_ms=telemetry.hrv_ms,
        resting_hr=telemetry.resting_hr,
    )
    budget = compute_daily_budget(
        base_capacity=settings.base_daily_capacity,
        busy_hours=telemetry.calendar_busy_hours,
        health=health,
        recent_telemetry=recent_telemetry,
        current_k_velocity=telemetry.k_velocity,
        settings=settings,
    )
    await session.commit()
    return build_daily_plan_view(
        day=today,
        tasks=tasks,
        candidates=candidates,
        budget=budget,
    )


@router.post("/tasks/{task_id}/toggle", response_model=TaskToggleResponse)
async def toggle_task(
    task_id: str,
    _token: str = Depends(require_bearer_token),
    session: AsyncSession = Depends(get_db_session),
) -> TaskToggleResponse:
    """Toggle a task between `active` and `done`."""
    repo = LifeOSRepository(session)
    task = await repo.toggle_task_done(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Task not found",
        )
    await session.commit()
    return TaskToggleResponse(
        task_id=task.id,
        status=task.status,
        completed_at=task.completed_at,
    )


@router.post("/candidates/{task_id}/decide", response_model=CandidateDecisionResponse)
async def decide_candidate(
    task_id: str,
    body: CandidateDecisionRequest,
    _token: str = Depends(require_bearer_token),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> CandidateDecisionResponse:
    """Approve a candidate into `active` tasks or mark it `dismissed`."""
    repo = LifeOSRepository(session)
    task = await repo.get_task(task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Candidate not found",
        )

    if body.approve:
        task.status = "active"
        task.confidence = 1.0
        task.confidence_tier = "high"
        if task.scheduled_date is None:
            task.scheduled_date = get_today_for_settings(settings)
    else:
        task.status = "dismissed"

    await session.commit()
    return CandidateDecisionResponse(
        task_id=task.id,
        status=task.status,
        approved=body.approve,
    )
