"""Initial schema with pgvector extension and LifeOS tables."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "raw_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("source_ref", sa.String(255), nullable=True, unique=True),
        sa.Column("content_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("modality", sa.String(24), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column("media_mime_type", sa.String(100), nullable=True),
        sa.Column("media_uri", sa.String(500), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_raw_items_source", "raw_items", ["source"])
    op.create_index("ix_raw_items_content_hash", "raw_items", ["content_hash"])

    op.create_table(
        "goals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("notion_page_id", sa.String(64), nullable=True, unique=True),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("horizon", sa.String(40), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("progress_pct", sa.Float(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "projects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="SET NULL")),
        sa.Column("notion_page_id", sa.String(64), nullable=True, unique=True),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("milestone", sa.String(240), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), sa.ForeignKey("projects.id", ondelete="SET NULL")),
        sa.Column("goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="SET NULL")),
        sa.Column("parent_task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="SET NULL")),
        sa.Column("raw_item_id", sa.String(36), sa.ForeignKey("raw_items.id", ondelete="SET NULL")),
        sa.Column("notion_page_id", sa.String(64), nullable=True, unique=True),
        sa.Column("apple_reminder_id", sa.String(128), nullable=True),
        sa.Column("caldav_uid", sa.String(128), nullable=True, unique=True),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("dod", sa.Text(), nullable=False),
        sa.Column("item_type", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("confidence_tier", sa.String(16), nullable=False),
        sa.Column("energy_tier", sa.String(16), nullable=False),
        sa.Column("duration_bucket", sa.Integer(), nullable=False),
        sa.Column("cognitive_load", sa.Integer(), nullable=False),
        sa.Column("effort", sa.Float(), nullable=False),
        sa.Column("urgency", sa.Integer(), nullable=False),
        sa.Column("day_anchor", sa.String(16), nullable=True),
        sa.Column("scheduled_date", sa.Date(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("has_exact_time", sa.Boolean(), nullable=False),
        sa.Column("deferral_count", sa.Integer(), nullable=False),
        sa.Column("source_link", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "daily_telemetry",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("day", sa.Date(), nullable=False, unique=True),
        sa.Column("notion_page_id", sa.String(64), nullable=True, unique=True),
        sa.Column("sleep_hours", sa.Float(), nullable=True),
        sa.Column("active_energy_kcal", sa.Float(), nullable=True),
        sa.Column("hrv_ms", sa.Float(), nullable=True),
        sa.Column("resting_hr", sa.Float(), nullable=True),
        sa.Column("calendar_busy_hours", sa.Float(), nullable=False),
        sa.Column("k_calendar", sa.Float(), nullable=False),
        sa.Column("k_health", sa.Float(), nullable=False),
        sa.Column("k_velocity", sa.Float(), nullable=False),
        sa.Column("daily_budget", sa.Float(), nullable=False),
        sa.Column("effort_planned", sa.Float(), nullable=False),
        sa.Column("effort_done", sa.Float(), nullable=False),
        sa.Column("completion_rate", sa.Float(), nullable=False),
        sa.Column("deferrals_count", sa.Integer(), nullable=False),
        sa.Column("overload_index", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "weekly_insights",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("week_start", sa.Date(), nullable=False, unique=True),
        sa.Column("notion_page_id", sa.String(64), nullable=True, unique=True),
        sa.Column("summary_markdown", sa.Text(), nullable=False),
        sa.Column("sleep_productivity_corr", sa.Float(), nullable=True),
        sa.Column("procrastination_patterns_json", sa.JSON(), nullable=False),
        sa.Column("goal_progress_json", sa.JSON(), nullable=False),
        sa.Column("recommendations_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    vector_type = Vector(768) if bind.dialect.name == "postgresql" else sa.Text()
    op.create_table(
        "embedding_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.String(36), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("model_name", sa.String(64), nullable=False),
        sa.Column("modality", sa.String(24), nullable=False),
        sa.Column("text_preview", sa.String(500), nullable=False),
        sa.Column("embedding", vector_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("entity_type", "entity_id", name="uq_embedding_entity"),
    )

    op.create_table(
        "sync_state",
        sa.Column("key", sa.String(120), primary_key=True),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("sync_state")
    op.drop_table("embedding_records")
    op.drop_table("weekly_insights")
    op.drop_table("daily_telemetry")
    op.drop_table("tasks")
    op.drop_table("projects")
    op.drop_table("goals")
    op.drop_table("raw_items")
