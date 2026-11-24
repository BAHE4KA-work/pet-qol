"""iOS Shortcuts sync and Siri quick-capture schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from lifeos.schemas.ai import DayAnchor


class ReminderSyncItem(BaseModel):
    """Reminder state pushed from Apple Reminders via iOS Shortcuts."""

    apple_reminder_id: str | None = None
    lifeos_task_id: str | None = None
    title: str
    notes: str | None = None
    is_completed: bool = False
    completed_at: datetime | None = None
    due_at: datetime | None = None


class NoteSyncItem(BaseModel):
    """Apple Note from selected LifeOS folder."""

    note_id: str
    title: str = ""
    body: str
    modified_at: datetime | None = None


class HealthMetricsPayload(BaseModel):
    """Aggregated Apple Health metrics over the past 24 hours."""

    sleep_hours: float | None = Field(default=None, ge=0.0, le=24.0)
    active_energy_kcal: float | None = Field(default=None, ge=0.0)
    hrv_ms: float | None = Field(default=None, ge=0.0)
    resting_hr: float | None = Field(default=None, ge=20.0, le=220.0)


class ShortcutsSyncRequest(BaseModel):
    """Payload sent by the adaptive iOS Shortcut `LifeOS Sync`."""

    synced_at: datetime | None = None
    focus_mode: str | None = None
    reminders: list[ReminderSyncItem] = Field(default_factory=list)
    notes: list[NoteSyncItem] = Field(default_factory=list)
    health: HealthMetricsPayload | None = None


class ReminderDiffAction(BaseModel):
    """Idempotent Reminder instruction returned to iOS Shortcuts."""

    action: Literal["upsert", "complete", "delete"]
    task_id: str
    lifeos_tag: str
    title: str
    notes: str
    day_anchor: DayAnchor | None = None
    due_at: datetime | None = None
    has_exact_time: bool = False


class ShortcutsSyncResponse(BaseModel):
    """Compact JSON diff returned to iPhone after sync."""

    processed_reminders: int = 0
    ingested_notes: int = 0
    health_updated: bool = False
    daily_budget: float
    reminders_diff: list[ReminderDiffAction] = Field(default_factory=list)


class SiriCaptureRequest(BaseModel):
    """Quick voice/text capture payload from Siri or iPhone Action Button."""

    text: str = Field(min_length=1, max_length=4000)
    captured_at: datetime | None = None


class SiriCaptureResponse(BaseModel):
    """Immediate confirmation returned to Siri / Action Button."""

    raw_item_id: str
    task_id: str | None = None
    status: str
    title: str | None = None
    confidence: float = 1.0
