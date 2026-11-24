"""ARQ background job implementations for LifeOS."""

from __future__ import annotations

import base64
import importlib
import inspect
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lifeos.ai.router import AIRouter
from lifeos.config import Settings, get_settings
from lifeos.db.models import RawItem, Task, WeeklyInsight
from lifeos.db.repositories import LifeOSRepository
from lifeos.integrations.caldav.client import ICloudCalDAVService
from lifeos.integrations.notion.client import NotionSyncService
from lifeos.logging import get_logger
from lifeos.planner.goldilocks import calculate_effort, compute_daily_budget
from lifeos.planner.routing import route_extracted_item
from lifeos.planner.scheduler import (
    apply_micro_step_split,
    find_tasks_needing_split,
    silent_rollover,
)
from lifeos.planner.weekly import WeeklyPlanningService
from lifeos.schemas.ai import WeeklyAnalyticsOutput, WeeklyPlanOutput
from lifeos.schemas.planner import HealthSnapshot

logger = get_logger(__name__)


def _get_settings(ctx: dict[str, Any]) -> Settings:
    cfg = ctx.get("settings")
    return cfg if isinstance(cfg, Settings) else get_settings()


def _get_session_factory(ctx: dict[str, Any]) -> async_sessionmaker[AsyncSession]:
    factory = ctx.get("session_factory")
    if factory is None:
        raise RuntimeError("Worker ctx missing 'session_factory'")
    return factory


def _resolve_tz(tz_name: str) -> tzinfo:
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return UTC


def _now(settings: Settings) -> datetime:
    return datetime.now(_resolve_tz(settings.timezone))


def _today(settings: Settings) -> date:
    return _now(settings).date()


def _is_notion_configured(settings: Settings, notion: NotionSyncService | None) -> bool:
    if notion is None:
        return False
    return bool(settings.notion_token) or getattr(notion, "_client", None) is not None


def _is_caldav_configured(settings: Settings, caldav_svc: ICloudCalDAVService | None) -> bool:
    if caldav_svc is None:
        return False
    has_creds = bool(settings.caldav_username) and bool(settings.caldav_app_password)
    return has_creds or getattr(caldav_svc, "_caldav_client", None) is not None


def _resolve_media_bytes(raw_item: RawItem) -> bytes | None:
    b64_payload = (raw_item.metadata_json or {}).get("media_bytes_b64")
    if isinstance(b64_payload, str) and b64_payload:
        try:
            return base64.b64decode(b64_payload)
        except Exception:
            logger.warning("Failed to decode media_bytes_b64 for RawItem %s", raw_item.id)
    uri = raw_item.media_uri or ""
    if uri.startswith("file://"):
        parsed = urlparse(uri)
        path = Path(parsed.path.lstrip("/") if parsed.path.startswith("/") else parsed.path)
        if path.is_file():
            return path.read_bytes()
    elif uri and not uri.startswith(("telegram://", "http://", "https://")):
        path = Path(uri)
        if path.is_file():
            return path.read_bytes()
    return None


async def _refresh_telegram_widget(ctx: dict[str, Any], settings: Settings) -> bool:
    bot = ctx.get("telegram_bot")
    if bot is None or settings.telegram_allowed_user_id <= 0:
        return False
    try:
        plan = await bot.build_today_plan()
        await bot.send_or_update_today_widget(settings.telegram_allowed_user_id, plan)
        return True
    except Exception as exc:
        logger.warning("Telegram widget refresh failed: %s", exc)
        return False


class _Tier3WeeklyAdapter:
    """Adapts Tier3HydrogenAgent methods to WeeklyPlanningService's expected interface."""

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    async def generate_weekly_plan(
        self,
        *,
        week_start: date,
        tasks: list[dict[str, Any]],
        goals: list[dict[str, Any]],
        calendar_slots: list[dict[str, Any]],
    ) -> WeeklyPlanOutput:
        return await self._agent.build_weekly_plan(
            week_start=week_start,
            active_tasks=tasks,
            goals=goals,
            calendar_slots=calendar_slots,
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
        _ = sleep_productivity_corr
        return await self._agent.generate_weekly_analytics(
            week_start=week_start,
            telemetry_rows=telemetry,
            stalled_tasks=tasks,
            goals=goals,
        )


def _build_ai_router(
    ctx: dict[str, Any],
    settings: Settings,
    repo: LifeOSRepository,
) -> AIRouter | None:
    factory = ctx.get("ai_router_factory")
    if callable(factory):
        return factory(repo)
    existing = ctx.get("ai_router")
    if existing is not None:
        return existing

    required = (
        "quota_manager",
        "embedding_service",
        "decision_service",
        "stt_service",
        "tier2_agent",
        "tier3_agent",
    )
    if any(ctx.get(key) is None for key in required):
        return None

    return AIRouter(
        settings=settings,
        repo=repo,
        quota_manager=ctx["quota_manager"],
        embedding_service=ctx["embedding_service"],
        decision_service=ctx["decision_service"],
        stt_service=ctx["stt_service"],
        tier2_agent=ctx["tier2_agent"],
        tier3_agent=ctx["tier3_agent"],
    )


def _parse_iso_datetime(raw: str | None) -> datetime | None:
    if not raw or "T" not in raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _parse_iso_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


async def process_raw_item(ctx: dict[str, Any], raw_item_id: str) -> dict[str, Any]:
    """Process one RawItem through AI triage/extraction, DB persistence, and downstream sync."""
    settings = _get_settings(ctx)
    session_factory = _get_session_factory(ctx)
    notion: NotionSyncService | None = ctx.get("notion_service")
    caldav_svc: ICloudCalDAVService | None = ctx.get("caldav_service")

    today = _today(settings)
    calendar_slots_payload: list[dict[str, Any]] | None = None
    if _is_caldav_configured(settings, caldav_svc) and caldav_svc is not None:
        try:
            tz = _resolve_tz(settings.timezone)
            day_start = datetime(today.year, today.month, today.day, tzinfo=tz)
            slots = await caldav_svc.fetch_busy_slots(day_start, day_start + timedelta(days=1))
            calendar_slots_payload = [slot.model_dump(mode="json") for slot in slots]
        except Exception as exc:
            logger.warning("CalDAV context fetch failed for raw_item=%s: %s", raw_item_id, exc)

    async with session_factory() as session:
        repo = LifeOSRepository(session)
        raw_item = await repo.get_raw_item(raw_item_id)
        if raw_item is None:
            return {"status": "not_found", "raw_item_id": raw_item_id}
        if raw_item.processed_at is not None:
            return {"status": "already_processed", "raw_item_id": raw_item_id}

        router = _build_ai_router(ctx, settings, repo)
        if router is None:
            logger.warning("AI router not configured; skipping raw_item=%s", raw_item_id)
            return {
                "status": "skipped",
                "reason": "ai_not_configured",
                "raw_item_id": raw_item_id,
            }

        triage, extracted = await router.process_raw_item(
            raw_item,
            media_bytes=_resolve_media_bytes(raw_item),
            calendar_slots=calendar_slots_payload,
        )
        if extracted is None:
            raw_item.processed_at = datetime.now(UTC)
            await session.commit()
            return {"status": "discarded", "raw_item_id": raw_item_id}

        confidence = float(triage.confidence) if triage is not None else 1.0
        routing = route_extracted_item(
            extracted,
            confidence=confidence,
            source=raw_item.source,
            settings=settings,
        )
        scheduled_date: date | None = None
        if extracted.due_at is not None:
            scheduled_date = extracted.due_at.date()
        elif routing["status"] == "active":
            scheduled_date = today

        task = await repo.create_task(
            raw_item_id=raw_item.id,
            goal_id=extracted.linked_goal_id,
            project_id=extracted.linked_project_id,
            title=extracted.title,
            dod=extracted.dod,
            item_type=extracted.item_type,
            status=routing["status"],
            confidence=confidence,
            confidence_tier=routing["confidence_tier"],
            energy_tier=extracted.energy_tier,
            duration_bucket=extracted.duration_bucket,
            cognitive_load=extracted.cognitive_load,
            effort=calculate_effort(extracted.duration_bucket, extracted.cognitive_load),
            urgency=extracted.urgency,
            day_anchor=extracted.suggested_anchor,
            scheduled_date=scheduled_date,
            due_at=extracted.due_at,
            has_exact_time=extracted.has_exact_time,
            source_link=(raw_item.metadata_json or {}).get("source_link"),
        )

        child_tasks: list[Task] = []
        for sub in extracted.subtasks:
            child = await repo.create_task(
                parent_task_id=task.id,
                raw_item_id=raw_item.id,
                goal_id=extracted.linked_goal_id,
                project_id=extracted.linked_project_id,
                title=sub.title,
                dod=sub.dod,
                item_type="task",
                status=routing["status"],
                confidence=confidence,
                confidence_tier=routing["confidence_tier"],
                energy_tier=sub.energy_tier,
                duration_bucket=sub.duration_bucket,
                cognitive_load=sub.cognitive_load,
                effort=calculate_effort(sub.duration_bucket, sub.cognitive_load),
                urgency=extracted.urgency,
                scheduled_date=scheduled_date,
                has_exact_time=False,
            )
            child_tasks.append(child)

        raw_item.processed_at = datetime.now(UTC)
        await session.commit()

        task_id = task.id
        subtask_count = len(child_tasks)
        notion_synced = False
        caldav_synced = False

        if routing["confidence_tier"] == "high":
            if (
                _is_notion_configured(settings, notion)
                and notion is not None
                and settings.notion_tasks_db_id
            ):
                try:
                    task.notion_page_id = await notion.push_task(task)
                    for child in child_tasks:
                        child.notion_page_id = await notion.push_task(child)
                    await session.commit()
                    notion_synced = True
                except Exception as exc:
                    await session.rollback()
                    logger.warning("Notion push failed for task=%s: %s", task_id, exc)

            if (
                extracted.item_type == "event"
                and routing["push_to_calendar"]
                and task.due_at is not None
                and _is_caldav_configured(settings, caldav_svc)
                and caldav_svc is not None
            ):
                try:
                    task.caldav_uid = await caldav_svc.upsert_event(task)
                    await session.commit()
                    caldav_synced = True
                except Exception as exc:
                    await session.rollback()
                    logger.warning("CalDAV event upsert failed for task=%s: %s", task_id, exc)

    await _refresh_telegram_widget(ctx, settings)
    return {
        "status": "processed",
        "raw_item_id": raw_item_id,
        "task_id": task_id,
        "subtask_count": subtask_count,
        "confidence_tier": routing["confidence_tier"],
        "notion_synced": notion_synced,
        "caldav_synced": caldav_synced,
    }


async def sync_notion_job(ctx: dict[str, Any]) -> dict[str, Any]:
    """Pull recent Notion Goal/Task changes into local DB and push dirty tasks."""
    settings = _get_settings(ctx)
    notion: NotionSyncService | None = ctx.get("notion_service")
    if not _is_notion_configured(settings, notion) or notion is None:
        return {"status": "skipped", "reason": "notion_not_configured"}

    session_factory = _get_session_factory(ctx)
    async with session_factory() as session:
        repo = LifeOSRepository(session)
        cursor_state = await repo.get_sync_state("notion_last_sync") or {}
        since = _parse_iso_datetime(cursor_state.get("last_synced_at"))

        changes = await notion.pull_recent_changes(since=since)

        goals_pulled = 0
        for g in changes.get("goals", []):
            await repo.upsert_goal(
                title=g.get("title") or "Untitled",
                notion_page_id=g.get("notion_page_id") or None,
                horizon=g.get("horizon") or "quarter",
                priority=int(g.get("priority") or 2),
                progress_pct=float(g.get("progress_pct") or 0.0),
                status=g.get("status") or "active",
            )
            goals_pulled += 1

        tasks_pulled = 0
        pulled_task_ids: set[str] = set()
        for t in changes.get("tasks", []):
            notion_page_id = t.get("notion_page_id") or None
            task: Task | None = None
            if t.get("lifeos_id"):
                task = await repo.get_task(str(t["lifeos_id"]))
            if task is None and notion_page_id:
                res = await session.execute(
                    select(Task).where(Task.notion_page_id == notion_page_id)
                )
                task = res.scalar_one_or_none()

            parsed_due_at = _parse_iso_datetime(t.get("due_at"))
            parsed_sched_date = _parse_iso_date(t.get("due_at"))
            status_val = (t.get("status") or "active").lower()

            if task is not None:
                if t.get("title"):
                    task.title = t["title"]
                if t.get("dod"):
                    task.dod = t["dod"]
                task.status = status_val
                if notion_page_id:
                    task.notion_page_id = notion_page_id
                if t.get("energy_tier"):
                    task.energy_tier = str(t["energy_tier"]).lower()
                if t.get("effort") is not None:
                    task.effort = float(t["effort"])
                if t.get("confidence") is not None:
                    task.confidence = float(t["confidence"])
                if t.get("day_anchor"):
                    task.day_anchor = str(t["day_anchor"]).lower()
                task.deferral_count = int(t.get("deferral_count") or 0)
                if parsed_due_at is not None:
                    task.due_at = parsed_due_at
                if parsed_sched_date is not None:
                    task.scheduled_date = parsed_sched_date
                if t.get("source_link"):
                    task.source_link = t["source_link"]
                if status_val == "done" and task.completed_at is None:
                    task.completed_at = datetime.now(UTC)
                elif status_val != "done":
                    task.completed_at = None
                pulled_task_ids.add(task.id)
                tasks_pulled += 1
            elif t.get("title"):
                created = await repo.create_task(
                    notion_page_id=notion_page_id,
                    title=t["title"],
                    dod=t.get("dod") or t["title"],
                    status=status_val,
                    confidence=float(t["confidence"]) if t.get("confidence") is not None else 1.0,
                    energy_tier=(t.get("energy_tier") or "medium").lower(),
                    effort=float(t["effort"]) if t.get("effort") is not None else 6.0,
                    day_anchor=t["day_anchor"].lower() if t.get("day_anchor") else None,
                    due_at=parsed_due_at,
                    scheduled_date=parsed_sched_date,
                    deferral_count=int(t.get("deferral_count") or 0),
                    source_link=t.get("source_link"),
                )
                pulled_task_ids.add(created.id)
                tasks_pulled += 1

        dirty_stmt = select(Task).where(Task.status.in_(["active", "candidate", "done"]))
        dirty_res = await session.execute(dirty_stmt)
        dirty_candidates = list(dirty_res.scalars().all())

        tasks_pushed = 0
        for local_task in dirty_candidates:
            if local_task.id in pulled_task_ids:
                continue
            needs_create = local_task.notion_page_id is None and bool(settings.notion_tasks_db_id)
            updated_at = local_task.updated_at
            if updated_at is not None and updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            needs_update = (
                local_task.notion_page_id is not None
                and since is not None
                and updated_at is not None
                and updated_at > since
            )
            if not (needs_create or needs_update):
                continue
            page_id = await notion.push_task(local_task)
            local_task.notion_page_id = page_id
            tasks_pushed += 1

        await repo.set_sync_state(
            "notion_last_sync",
            {"last_synced_at": datetime.now(UTC).isoformat()},
        )
        await session.commit()

    return {
        "status": "ok",
        "goals_pulled": goals_pulled,
        "tasks_pulled": tasks_pulled,
        "tasks_pushed": tasks_pushed,
    }


async def sync_caldav_job(ctx: dict[str, Any]) -> dict[str, Any]:
    """Fetch CalDAV busy slots, update calendar_busy_hours, and recalculate daily budget."""
    settings = _get_settings(ctx)
    caldav_svc: ICloudCalDAVService | None = ctx.get("caldav_service")
    if not _is_caldav_configured(settings, caldav_svc) or caldav_svc is None:
        return {"status": "skipped", "reason": "caldav_not_configured"}

    tz = _resolve_tz(settings.timezone)
    today = _today(settings)
    day_start = datetime(today.year, today.month, today.day, tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    week_end = day_start + timedelta(days=7)

    week_slots = await caldav_svc.fetch_busy_slots(day_start, week_end)
    busy_hours = 0.0
    for slot in week_slots:
        s_at = (
            slot.start_at if slot.start_at.tzinfo is not None else slot.start_at.replace(tzinfo=tz)
        )
        e_at = slot.end_at if slot.end_at.tzinfo is not None else slot.end_at.replace(tzinfo=tz)
        overlap_start = max(s_at, day_start)
        overlap_end = min(e_at, day_end)
        if overlap_end > overlap_start:
            busy_hours += (overlap_end - overlap_start).total_seconds() / 3600.0

    session_factory = _get_session_factory(ctx)
    async with session_factory() as session:
        repo = LifeOSRepository(session)
        telemetry = await repo.get_or_create_telemetry(today)
        recent = await repo.list_recent_telemetry(days=14, up_to_day=today)

        health: HealthSnapshot | None = None
        if any(
            v is not None
            for v in (
                telemetry.sleep_hours,
                telemetry.active_energy_kcal,
                telemetry.hrv_ms,
                telemetry.resting_hr,
            )
        ):
            health = HealthSnapshot(
                sleep_hours=telemetry.sleep_hours,
                active_energy_kcal=telemetry.active_energy_kcal,
                hrv_ms=telemetry.hrv_ms,
                resting_hr=telemetry.resting_hr,
            )

        breakdown = compute_daily_budget(
            base_capacity=settings.base_daily_capacity,
            busy_hours=busy_hours,
            health=health,
            recent_telemetry=recent,
            current_k_velocity=telemetry.k_velocity,
            settings=settings,
        )
        telemetry.calendar_busy_hours = round(busy_hours, 2)
        telemetry.k_calendar = breakdown.k_calendar
        telemetry.k_health = breakdown.k_health
        telemetry.k_velocity = breakdown.k_velocity
        telemetry.daily_budget = breakdown.daily_budget
        await session.commit()

    return {
        "status": "ok",
        "day": today.isoformat(),
        "busy_hours": round(busy_hours, 2),
        "k_calendar": breakdown.k_calendar,
        "daily_budget": breakdown.daily_budget,
        "week_slots_count": len(week_slots),
    }


async def poll_gmail_job(ctx: dict[str, Any]) -> dict[str, Any]:
    """Run GmailReadOnlyPoller.poll_once() if Gmail credentials are configured."""
    settings = _get_settings(ctx)
    poller = ctx.get("gmail_poller")
    if poller is None:
        if not (settings.gmail_credentials_json or settings.gmail_token_json):
            return {"status": "skipped", "reason": "gmail_not_configured"}
        try:
            gmail_mod = importlib.import_module("lifeos.integrations.gmail.poller")
            poller_cls = gmail_mod.GmailReadOnlyPoller
        except (ImportError, AttributeError) as exc:
            logger.warning("GmailReadOnlyPoller unavailable: %s", exc)
            return {"status": "skipped", "reason": "gmail_module_unavailable"}
        kwargs: dict[str, Any] = {"settings": settings}
        if "session_factory" in inspect.signature(poller_cls.__init__).parameters:
            kwargs["session_factory"] = ctx.get("session_factory")
        poller = poller_cls(**kwargs)
        ctx["gmail_poller"] = poller

    raw_result = poller.poll_once()
    if inspect.isawaitable(raw_result):
        raw_result = await raw_result
    return {"status": "ok", "result": raw_result}


async def daily_rollover_job(ctx: dict[str, Any]) -> dict[str, Any]:
    """Roll over unfinished tasks, micro-split stalled tasks, save telemetry & update widget."""
    settings = _get_settings(ctx)
    session_factory = _get_session_factory(ctx)
    tier2_agent = ctx.get("tier2_agent")
    notion: NotionSyncService | None = ctx.get("notion_service")
    today = _today(settings)

    split_count = 0
    notion_synced = False
    async with session_factory() as session:
        repo = LifeOSRepository(session)
        unfinished = await repo.list_unfinished_before(today)
        rolled = silent_rollover(unfinished, today)
        await session.flush()

        today_tasks = await repo.list_tasks_for_date(today)
        stalled = find_tasks_needing_split(
            today_tasks,
            threshold=settings.deferral_split_threshold,
        )

        if tier2_agent is not None:
            for task in stalled:
                existing_children = await session.execute(
                    select(Task.id).where(Task.parent_task_id == task.id).limit(1)
                )
                if existing_children.scalar_one_or_none() is not None:
                    continue
                subtasks = await tier2_agent.split_stalled_task(
                    task.title,
                    task.dod,
                    task.deferral_count,
                )
                if subtasks:
                    await apply_micro_step_split(repo, task, subtasks, scheduled_date=today)
                    split_count += 1

        await session.flush()
        refreshed_tasks = await repo.list_tasks_for_date(today)
        effort_planned = sum(float(t.effort) for t in refreshed_tasks)
        effort_done = sum(float(t.effort) for t in refreshed_tasks if t.status == "done")
        completion_rate = (effort_done / effort_planned) if effort_planned > 0.0 else 0.0

        telemetry = await repo.get_or_create_telemetry(today)
        telemetry.effort_planned = round(effort_planned, 2)
        telemetry.effort_done = round(effort_done, 2)
        telemetry.completion_rate = round(completion_rate, 4)
        telemetry.deferrals_count = len(rolled)
        budget_val = float(telemetry.daily_budget or settings.base_daily_capacity)
        telemetry.overload_index = round(effort_planned / budget_val, 4) if budget_val > 0 else 0.0
        await session.commit()

        if (
            _is_notion_configured(settings, notion)
            and notion is not None
            and (telemetry.notion_page_id or settings.notion_telemetry_db_id)
        ):
            try:
                telemetry.notion_page_id = await notion.push_daily_telemetry(telemetry)
                await session.commit()
                notion_synced = True
            except Exception as exc:
                await session.rollback()
                logger.warning("Notion telemetry push failed: %s", exc)

    await _refresh_telegram_widget(ctx, settings)
    return {
        "status": "ok",
        "day": today.isoformat(),
        "rolled_over": len(rolled),
        "split_tasks": split_count,
        "notion_synced": notion_synced,
    }


async def weekly_plan_and_analytics_job(ctx: dict[str, Any]) -> dict[str, Any]:
    """Run WeeklyPlanningService using reserved Tier 3 Hydrogen quota and sync to Notion."""
    settings = _get_settings(ctx)
    tier3_agent = ctx.get("tier3_agent")
    if tier3_agent is None:
        logger.warning("Tier 3 Hydrogen agent not configured; skipping weekly cycle")
        return {"status": "skipped", "reason": "tier3_not_configured"}

    today = _today(settings)
    week_start = ctx.get("week_start")
    if not isinstance(week_start, date):
        if today.weekday() == 6:
            week_start = today + timedelta(days=1)
        else:
            week_start = today - timedelta(days=today.weekday())

    caldav_svc: ICloudCalDAVService | None = ctx.get("caldav_service")
    calendar_slots: list[dict[str, Any]] = []
    if _is_caldav_configured(settings, caldav_svc) and caldav_svc is not None:
        try:
            tz = _resolve_tz(settings.timezone)
            start_dt = datetime(week_start.year, week_start.month, week_start.day, tzinfo=tz)
            slots = await caldav_svc.fetch_busy_slots(start_dt, start_dt + timedelta(days=7))
            calendar_slots = [slot.model_dump(mode="json") for slot in slots]
        except Exception as exc:
            logger.warning("CalDAV weekly fetch failed: %s", exc)

    adapted_agent = (
        tier3_agent
        if hasattr(tier3_agent, "generate_weekly_plan")
        else _Tier3WeeklyAdapter(tier3_agent)
    )

    session_factory = _get_session_factory(ctx)
    notion: NotionSyncService | None = ctx.get("notion_service")
    notion_synced = False
    async with session_factory() as session:
        repo = LifeOSRepository(session)
        service = WeeklyPlanningService(repo=repo, tier3_agent=adapted_agent)
        plan, _analytics = await service.run_weekly_cycle(
            week_start=week_start,
            calendar_slots=calendar_slots,
        )
        await session.commit()

        insight_res = await session.execute(
            select(WeeklyInsight).where(WeeklyInsight.week_start == week_start)
        )
        insight = insight_res.scalar_one_or_none()
        if (
            insight is not None
            and _is_notion_configured(settings, notion)
            and notion is not None
            and (insight.notion_page_id or settings.notion_insights_db_id)
        ):
            try:
                insight.notion_page_id = await notion.push_weekly_insight(insight)
                await session.commit()
                notion_synced = True
            except Exception as exc:
                await session.rollback()
                logger.warning("Notion weekly insight push failed: %s", exc)

    return {
        "status": "ok",
        "week_start": week_start.isoformat(),
        "slots_scheduled": len(plan.slots),
        "notion_synced": notion_synced,
    }
