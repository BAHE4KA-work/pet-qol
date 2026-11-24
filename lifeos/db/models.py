"""SQLAlchemy ORM models for LifeOS."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from lifeos.db.base import Base, EmbeddingVector


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RawItem(Base):
    """Raw incoming fragment from Telegram, Gmail, Apple Notes, Siri, or Notion."""

    __tablename__ = "raw_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    source: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    modality: Mapped[str] = mapped_column(String(24), nullable=False, default="text")
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_mime_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    media_uri: Mapped[str | None] = mapped_column(String(500), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class Goal(Base):
    """Global Goal synced with Notion Global Goals database."""

    __tablename__ = "goals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    notion_page_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    horizon: Mapped[str] = mapped_column(String(40), nullable=False, default="quarter")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    progress_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    projects: Mapped[list[Project]] = relationship(back_populates="goal")


class Project(Base):
    """Project linked to a Global Goal in Notion."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    goal_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("goals.id", ondelete="SET NULL"), nullable=True
    )
    notion_page_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    milestone: Mapped[str | None] = mapped_column(String(240), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    goal: Mapped[Goal | None] = relationship(back_populates="projects")


class Task(Base):
    """Actionable task, event, subtask, candidate, or someday idea."""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    goal_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("goals.id", ondelete="SET NULL"), nullable=True
    )
    parent_task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    raw_item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("raw_items.id", ondelete="SET NULL"), nullable=True
    )
    notion_page_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    apple_reminder_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    caldav_uid: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)

    title: Mapped[str] = mapped_column(String(240), nullable=False)
    dod: Mapped[str] = mapped_column(Text, nullable=False)
    item_type: Mapped[str] = mapped_column(String(24), nullable=False, default="task", index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active", index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    confidence_tier: Mapped[str] = mapped_column(String(16), nullable=False, default="high")
    energy_tier: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    duration_bucket: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    cognitive_load: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    effort: Mapped[float] = mapped_column(Float, nullable=False, default=6.0)
    urgency: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    day_anchor: Mapped[str | None] = mapped_column(String(16), nullable=True)
    scheduled_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    has_exact_time: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deferral_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_link: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DailyTelemetry(Base):
    """Passive daily telemetry record synced with Notion Daily Telemetry."""

    __tablename__ = "daily_telemetry"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    day: Mapped[date] = mapped_column(Date, nullable=False, unique=True, index=True)
    notion_page_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)

    sleep_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    active_energy_kcal: Mapped[float | None] = mapped_column(Float, nullable=True)
    hrv_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    resting_hr: Mapped[float | None] = mapped_column(Float, nullable=True)
    calendar_busy_hours: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    k_calendar: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    k_health: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    k_velocity: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    daily_budget: Mapped[float] = mapped_column(Float, nullable=False, default=20.0)

    effort_planned: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    effort_done: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    completion_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    deferrals_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    overload_index: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class WeeklyInsight(Base):
    """Weekly analytical digest generated by Tier 3 (gemini-4-hydrogen)."""

    __tablename__ = "weekly_insights"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    week_start: Mapped[date] = mapped_column(Date, nullable=False, unique=True, index=True)
    notion_page_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    summary_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    sleep_productivity_corr: Mapped[float | None] = mapped_column(Float, nullable=True)
    procrastination_patterns_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    goal_progress_json: Mapped[dict[str, str]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    recommendations_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class EmbeddingRecord(Base):
    """Unified gemini-embedding-2 vector index across text, images, audio, and docs."""

    __tablename__ = "embedding_records"
    __table_args__ = (
        UniqueConstraint("entity_type", "entity_id", name="uq_embedding_entity"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    model_name: Mapped[str] = mapped_column(
        String(64), nullable=False, default="gemini-embedding-2"
    )
    modality: Mapped[str] = mapped_column(String(24), nullable=False, default="text")
    text_preview: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    embedding: Mapped[list[float]] = mapped_column(EmbeddingVector(768), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class SyncState(Base):
    """Key-value state cursor for integrations (Gmail historyId, TG widget message_id, etc.)."""

    __tablename__ = "sync_state"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
