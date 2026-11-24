"""Async repository layer for LifeOS database operations and RAG search."""

from __future__ import annotations

import hashlib
import math
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lifeos.db.models import (
    DailyTelemetry,
    EmbeddingRecord,
    Goal,
    Project,
    RawItem,
    SyncState,
    Task,
    WeeklyInsight,
)


def compute_sha256(content: bytes | str) -> str:
    """Compute SHA-256 hex digest for deduplication."""
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(raw).hexdigest()


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class LifeOSRepository:
    """Async repository encapsulating DB queries, dedup, and vector search."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- RawItem ---
    async def get_raw_item_by_hash(self, content_hash: str) -> RawItem | None:
        res = await self.session.execute(
            select(RawItem).where(RawItem.content_hash == content_hash)
        )
        return res.scalar_one_or_none()

    async def get_raw_item(self, raw_item_id: str) -> RawItem | None:
        return await self.session.get(RawItem, raw_item_id)

    async def create_raw_item_if_new(
        self,
        *,
        source: str,
        content_hash: str,
        modality: str = "text",
        raw_text: str | None = None,
        source_ref: str | None = None,
        media_mime_type: str | None = None,
        media_uri: str | None = None,
        metadata_json: dict[str, Any] | None = None,
    ) -> tuple[RawItem, bool]:
        existing = await self.get_raw_item_by_hash(content_hash)
        if existing is not None:
            return existing, False
        if source_ref:
            res = await self.session.execute(
                select(RawItem).where(RawItem.source_ref == source_ref)
            )
            by_ref = res.scalar_one_or_none()
            if by_ref is not None:
                return by_ref, False
        item = RawItem(
            source=source,
            source_ref=source_ref,
            content_hash=content_hash,
            modality=modality,
            raw_text=raw_text,
            media_mime_type=media_mime_type,
            media_uri=media_uri,
            metadata_json=metadata_json or {},
        )
        self.session.add(item)
        await self.session.flush()
        return item, True

    # --- Goals & Projects ---
    async def list_active_goals(self) -> list[Goal]:
        res = await self.session.execute(select(Goal).where(Goal.status == "active"))
        return list(res.scalars().all())

    async def upsert_goal(
        self,
        *,
        title: str,
        notion_page_id: str | None = None,
        horizon: str = "quarter",
        priority: int = 2,
        progress_pct: float = 0.0,
        status: str = "active",
    ) -> Goal:
        goal: Goal | None = None
        if notion_page_id:
            res = await self.session.execute(
                select(Goal).where(Goal.notion_page_id == notion_page_id)
            )
            goal = res.scalar_one_or_none()
        if goal is None:
            goal = Goal(
                title=title,
                notion_page_id=notion_page_id,
                horizon=horizon,
                priority=priority,
                progress_pct=progress_pct,
                status=status,
            )
            self.session.add(goal)
        else:
            goal.title = title
            goal.horizon = horizon
            goal.priority = priority
            goal.progress_pct = progress_pct
            goal.status = status
        await self.session.flush()
        return goal

    async def upsert_project(
        self,
        *,
        title: str,
        goal_id: str | None = None,
        notion_page_id: str | None = None,
        status: str = "active",
        milestone: str | None = None,
    ) -> Project:
        project: Project | None = None
        if notion_page_id:
            res = await self.session.execute(
                select(Project).where(Project.notion_page_id == notion_page_id)
            )
            project = res.scalar_one_or_none()
        if project is None:
            project = Project(
                title=title,
                goal_id=goal_id,
                notion_page_id=notion_page_id,
                status=status,
                milestone=milestone,
            )
            self.session.add(project)
        else:
            project.title = title
            project.goal_id = goal_id
            project.status = status
            project.milestone = milestone
        await self.session.flush()
        return project

    # --- Tasks ---
    async def get_task(self, task_id: str) -> Task | None:
        return await self.session.get(Task, task_id)

    async def create_task(self, **kwargs: Any) -> Task:
        if "effort" not in kwargs:
            dur = int(kwargs.get("duration_bucket", 2))
            cog = int(kwargs.get("cognitive_load", 3))
            kwargs["effort"] = float(dur * cog)
        task = Task(**kwargs)
        self.session.add(task)
        await self.session.flush()
        return task

    async def list_tasks_for_date(self, day: date) -> list[Task]:
        res = await self.session.execute(
            select(Task).where(
                Task.scheduled_date == day,
                Task.status.in_(["active", "done"]),
            )
        )
        return list(res.scalars().all())

    async def list_active_tasks(self) -> list[Task]:
        res = await self.session.execute(select(Task).where(Task.status == "active"))
        return list(res.scalars().all())

    async def list_candidates(self) -> list[Task]:
        res = await self.session.execute(select(Task).where(Task.status == "candidate"))
        return list(res.scalars().all())

    async def list_unfinished_before(self, day: date) -> list[Task]:
        res = await self.session.execute(
            select(Task).where(
                Task.status == "active",
                Task.scheduled_date.is_not(None),
                Task.scheduled_date < day,
            )
        )
        return list(res.scalars().all())

    async def toggle_task_done(self, task_id: str) -> Task | None:
        task = await self.get_task(task_id)
        if task is None:
            return None
        if task.status == "done":
            task.status = "active"
            task.completed_at = None
        else:
            task.status = "done"
            task.completed_at = datetime.now(UTC)
        await self.session.flush()
        return task

    # --- DailyTelemetry & WeeklyInsight ---
    async def get_or_create_telemetry(self, day: date) -> DailyTelemetry:
        res = await self.session.execute(
            select(DailyTelemetry).where(DailyTelemetry.day == day)
        )
        rec = res.scalar_one_or_none()
        if rec is None:
            rec = DailyTelemetry(day=day)
            self.session.add(rec)
            await self.session.flush()
        return rec

    async def list_recent_telemetry(
        self, *, days: int = 14, up_to_day: date | None = None
    ) -> list[DailyTelemetry]:
        end_day = up_to_day or date.today()
        start_day = end_day - timedelta(days=days)
        res = await self.session.execute(
            select(DailyTelemetry)
            .where(DailyTelemetry.day >= start_day, DailyTelemetry.day <= end_day)
            .order_by(DailyTelemetry.day.asc())
        )
        return list(res.scalars().all())

    async def upsert_weekly_insight(
        self,
        *,
        week_start: date,
        summary_markdown: str,
        sleep_productivity_corr: float | None = None,
        procrastination_patterns: list[str] | None = None,
        goal_progress: dict[str, str] | None = None,
        recommendations: list[str] | None = None,
    ) -> WeeklyInsight:
        res = await self.session.execute(
            select(WeeklyInsight).where(WeeklyInsight.week_start == week_start)
        )
        rec = res.scalar_one_or_none()
        if rec is None:
            rec = WeeklyInsight(
                week_start=week_start,
                summary_markdown=summary_markdown,
                sleep_productivity_corr=sleep_productivity_corr,
                procrastination_patterns_json=procrastination_patterns or [],
                goal_progress_json=goal_progress or {},
                recommendations_json=recommendations or [],
            )
            self.session.add(rec)
        else:
            rec.summary_markdown = summary_markdown
            rec.sleep_productivity_corr = sleep_productivity_corr
            rec.procrastination_patterns_json = procrastination_patterns or []
            rec.goal_progress_json = goal_progress or {}
            rec.recommendations_json = recommendations or []
        await self.session.flush()
        return rec

    # --- Embeddings & RAG ---
    async def get_embedding_by_hash(self, content_hash: str) -> EmbeddingRecord | None:
        res = await self.session.execute(
            select(EmbeddingRecord).where(EmbeddingRecord.content_hash == content_hash)
        )
        return res.scalar_one_or_none()

    async def upsert_embedding(
        self,
        *,
        entity_type: str,
        entity_id: str,
        content_hash: str,
        embedding: list[float],
        model_name: str = "gemini-embedding-2",
        modality: str = "text",
        text_preview: str = "",
    ) -> EmbeddingRecord:
        if model_name != "gemini-embedding-2":
            raise ValueError(
                f"Incompatible embedding model '{model_name}': only 'gemini-embedding-2' is allowed"
            )
        res = await self.session.execute(
            select(EmbeddingRecord).where(
                EmbeddingRecord.entity_type == entity_type,
                EmbeddingRecord.entity_id == entity_id,
            )
        )
        rec = res.scalar_one_or_none()
        if rec is None:
            rec = EmbeddingRecord(
                entity_type=entity_type,
                entity_id=entity_id,
                content_hash=content_hash,
                model_name=model_name,
                modality=modality,
                text_preview=text_preview[:500],
                embedding=embedding,
            )
            self.session.add(rec)
        else:
            rec.content_hash = content_hash
            rec.embedding = embedding
            rec.modality = modality
            rec.text_preview = text_preview[:500]
        await self.session.flush()
        return rec

    async def semantic_search(
        self,
        query_vector: list[float],
        *,
        limit: int = 5,
        entity_types: list[str] | None = None,
    ) -> list[tuple[EmbeddingRecord, float]]:
        dialect_name = self.session.bind.dialect.name if self.session.bind else "sqlite"
        stmt = select(EmbeddingRecord)
        if entity_types:
            stmt = stmt.where(EmbeddingRecord.entity_type.in_(entity_types))

        if dialect_name == "postgresql":
            dist_expr = EmbeddingRecord.embedding.cosine_distance(query_vector)
            pg_stmt = select(EmbeddingRecord, (1.0 - dist_expr).label("similarity"))
            if entity_types:
                pg_stmt = pg_stmt.where(EmbeddingRecord.entity_type.in_(entity_types))
            pg_stmt = pg_stmt.order_by(dist_expr.asc()).limit(limit)
            res = await self.session.execute(pg_stmt)
            return [(row[0], float(row[1])) for row in res.all()]

        res = await self.session.execute(stmt)
        records = list(res.scalars().all())
        scored = [(rec, _cosine_similarity(query_vector, rec.embedding)) for rec in records]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:limit]

    # --- SyncState ---
    async def get_sync_state(self, key: str) -> dict[str, Any] | None:
        rec = await self.session.get(SyncState, key)
        return rec.value_json if rec else None

    async def set_sync_state(self, key: str, value_json: dict[str, Any]) -> SyncState:
        rec = await self.session.get(SyncState, key)
        if rec is None:
            rec = SyncState(key=key, value_json=value_json)
            self.session.add(rec)
        else:
            rec.value_json = value_json
        await self.session.flush()
        return rec
