"""iOS Shortcuts bidirectional sync and Siri quick-capture endpoints."""

from __future__ import annotations

import inspect
import re
from datetime import UTC, datetime, timedelta
from typing import cast

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lifeos.api.auth import get_db_session, get_today_for_settings, require_bearer_token
from lifeos.config import Settings, get_settings
from lifeos.db.models import Task
from lifeos.db.repositories import LifeOSRepository, compute_sha256
from lifeos.planner.goldilocks import compute_daily_budget
from lifeos.schemas.ai import DayAnchor
from lifeos.schemas.planner import HealthSnapshot
from lifeos.schemas.shortcuts import (
    ReminderDiffAction,
    ShortcutsSyncRequest,
    ShortcutsSyncResponse,
    SiriCaptureRequest,
    SiriCaptureResponse,
)

router = APIRouter(prefix="/api/v1/shortcuts", tags=["shortcuts"])

_LIFEOS_TAG_RE = re.compile(r"\[lifeos:([^\s\]]+)\]")
_VALID_ANCHORS: set[str] = {"morning", "afternoon", "evening"}


def extract_lifeos_tag(notes: str | None) -> str | None:
    """Extract task ID from an embedded `[lifeos:<task_id>]` tag in reminder notes."""
    if not notes:
        return None
    match = _LIFEOS_TAG_RE.search(notes)
    return match.group(1) if match else None


async def _enqueue_if_set(request: Request, raw_item_id: str) -> None:
    enqueue_fn = getattr(request.app.state, "enqueue_fn", None)
    if enqueue_fn is None:
        return
    result = enqueue_fn(raw_item_id)
    if inspect.isawaitable(result):
        await result


@router.post("/sync", response_model=ShortcutsSyncResponse)
async def shortcuts_sync(
    payload: ShortcutsSyncRequest,
    request: Request,
    _token: str = Depends(require_bearer_token),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> ShortcutsSyncResponse:
    """Sync Apple Reminders, Notes, and HealthKit telemetry with LifeOS."""
    repo = LifeOSRepository(session)
    today = get_today_for_settings(settings)
    tomorrow = today + timedelta(days=1)

    processed_reminders = 0
    for rem in payload.reminders:
        task_id = rem.lifeos_task_id or extract_lifeos_tag(rem.notes)
        task: Task | None = await repo.get_task(task_id) if task_id else None
        if task is None and rem.apple_reminder_id:
            res = await session.execute(
                select(Task).where(Task.apple_reminder_id == rem.apple_reminder_id)
            )
            task = res.scalars().first()
        if task is None:
            continue

        if rem.apple_reminder_id and task.apple_reminder_id != rem.apple_reminder_id:
            task.apple_reminder_id = rem.apple_reminder_id
        if rem.is_completed:
            task.status = "done"
            task.completed_at = rem.completed_at or datetime.now(UTC)
        processed_reminders += 1

    ingested_notes = 0
    new_raw_item_ids: list[str] = []
    for note in payload.notes:
        raw_text = f"{note.title}\n{note.body}"
        content_hash = compute_sha256(raw_text)
        raw_item, created = await repo.create_raw_item_if_new(
            source="apple_notes",
            source_ref=f"note:{note.note_id}",
            content_hash=content_hash,
            modality="note",
            raw_text=raw_text,
            metadata_json={
                "note_id": note.note_id,
                "title": note.title,
                "modified_at": note.modified_at.isoformat() if note.modified_at else None,
            },
        )
        if created:
            ingested_notes += 1
            new_raw_item_ids.append(raw_item.id)

    telemetry = await repo.get_or_create_telemetry(today)
    health_updated = False
    if payload.health is not None:
        if payload.health.sleep_hours is not None:
            telemetry.sleep_hours = payload.health.sleep_hours
        if payload.health.active_energy_kcal is not None:
            telemetry.active_energy_kcal = payload.health.active_energy_kcal
        if payload.health.hrv_ms is not None:
            telemetry.hrv_ms = payload.health.hrv_ms
        if payload.health.resting_hr is not None:
            telemetry.resting_hr = payload.health.resting_hr
        health_updated = True

    health_snapshot = HealthSnapshot(
        sleep_hours=telemetry.sleep_hours,
        active_energy_kcal=telemetry.active_energy_kcal,
        hrv_ms=telemetry.hrv_ms,
        resting_hr=telemetry.resting_hr,
    )
    recent_telemetry = await repo.list_recent_telemetry(days=14, up_to_day=today)
    budget = compute_daily_budget(
        base_capacity=settings.base_daily_capacity,
        busy_hours=telemetry.calendar_busy_hours,
        health=health_snapshot,
        recent_telemetry=recent_telemetry,
        current_k_velocity=telemetry.k_velocity,
        settings=settings,
    )
    telemetry.k_calendar = budget.k_calendar
    telemetry.k_health = budget.k_health
    telemetry.k_velocity = budget.k_velocity
    telemetry.daily_budget = budget.daily_budget

    tasks_res = await session.execute(
        select(Task)
        .where(
            Task.status == "active",
            Task.item_type == "task",
            Task.scheduled_date.in_([today, tomorrow]),
        )
        .order_by(Task.scheduled_date.asc(), Task.urgency.desc())
    )
    reminders_diff: list[ReminderDiffAction] = []
    for task in tasks_res.scalars().all():
        tag = f"[lifeos:{task.id}]"
        anchor = cast(
            DayAnchor | None,
            task.day_anchor if task.day_anchor in _VALID_ANCHORS else None,
        )
        reminders_diff.append(
            ReminderDiffAction(
                action="upsert",
                task_id=task.id,
                lifeos_tag=tag,
                title=task.title,
                notes=f"{task.dod}\n\n{tag}",
                day_anchor=anchor,
                due_at=task.due_at if task.has_exact_time else None,
                has_exact_time=task.has_exact_time,
            )
        )

    await session.commit()

    for raw_item_id in new_raw_item_ids:
        await _enqueue_if_set(request, raw_item_id)

    return ShortcutsSyncResponse(
        processed_reminders=processed_reminders,
        ingested_notes=ingested_notes,
        health_updated=health_updated,
        daily_budget=budget.daily_budget,
        reminders_diff=reminders_diff,
    )


@router.post("/siri", response_model=SiriCaptureResponse)
async def siri_capture(
    req: SiriCaptureRequest,
    request: Request,
    _token: str = Depends(require_bearer_token),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> SiriCaptureResponse:
    """Capture a direct Siri or Action Button task with confidence=1.0."""
    repo = LifeOSRepository(session)
    today = get_today_for_settings(settings)
    text = req.text.strip()
    captured_suffix = req.captured_at.isoformat() if req.captured_at else ""
    hash_seed = f"siri|{text}|{captured_suffix}" if captured_suffix else f"siri|{text}"

    raw_item, created = await repo.create_raw_item_if_new(
        source="siri",
        content_hash=compute_sha256(hash_seed),
        modality="text",
        raw_text=text,
        metadata_json={
            "confidence": 1.0,
            "captured_at": req.captured_at.isoformat() if req.captured_at else None,
        },
    )

    if not created:
        existing_res = await session.execute(select(Task).where(Task.raw_item_id == raw_item.id))
        existing_task = existing_res.scalars().first()
        if existing_task is not None:
            return SiriCaptureResponse(
                raw_item_id=raw_item.id,
                task_id=existing_task.id,
                status=existing_task.status,
                title=existing_task.title,
                confidence=1.0,
            )

    ai_router = getattr(request.app.state, "ai_router", None)
    task: Task | None = None
    if ai_router is not None:
        _, extracted = await ai_router.process_raw_item(raw_item)
        raw_item.processed_at = datetime.now(UTC)
        if extracted is not None:
            task = await repo.create_task(
                raw_item_id=raw_item.id,
                project_id=extracted.linked_project_id,
                goal_id=extracted.linked_goal_id,
                title=extracted.title,
                dod=extracted.dod,
                item_type=extracted.item_type,
                status="active",
                confidence=1.0,
                confidence_tier="high",
                energy_tier=extracted.energy_tier,
                duration_bucket=extracted.duration_bucket,
                cognitive_load=extracted.cognitive_load,
                effort=float(extracted.duration_bucket * extracted.cognitive_load),
                urgency=extracted.urgency,
                day_anchor=extracted.suggested_anchor,
                scheduled_date=today,
                due_at=extracted.due_at,
                has_exact_time=extracted.has_exact_time,
            )

    if task is None:
        task = await repo.create_task(
            raw_item_id=raw_item.id,
            title=text[:240],
            dod=text[:500],
            item_type="task",
            status="active",
            confidence=1.0,
            confidence_tier="high",
            scheduled_date=today,
        )

    await session.commit()

    if ai_router is None and created:
        await _enqueue_if_set(request, raw_item.id)

    return SiriCaptureResponse(
        raw_item_id=raw_item.id,
        task_id=task.id,
        status=task.status,
        title=task.title,
        confidence=1.0,
    )
