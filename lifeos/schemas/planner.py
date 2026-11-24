"""Planner, Goldilocks budget, calendar, and Telegram widget view schemas."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field

from lifeos.schemas.ai import ConfidenceTier, DayAnchor, EnergyTier, ItemType


class HealthSnapshot(BaseModel):
    """Aggregated Apple Health telemetry for daily budget calibration."""

    sleep_hours: float | None = Field(default=None, ge=0.0, le=24.0)
    active_energy_kcal: float | None = Field(default=None, ge=0.0)
    hrv_ms: float | None = Field(default=None, ge=0.0)
    resting_hr: float | None = Field(default=None, ge=20.0, le=220.0)


class CalendarSlot(BaseModel):
    """Busy event block from Apple iCloud CalDAV."""

    uid: str
    title: str
    start_at: datetime
    end_at: datetime

    @property
    def duration_hours(self) -> float:
        delta = (self.end_at - self.start_at).total_seconds() / 3600.0
        return max(0.0, delta)


class BudgetBreakdown(BaseModel):
    """Goldilocks daily capacity budget breakdown."""

    base_capacity: float
    k_calendar: float
    k_health: float
    k_velocity: float
    daily_budget: float
    strip_deep_tasks: bool = False
    rolling_completion_rate: float | None = None


class TaskCardDTO(BaseModel):
    """Compact task representation for Telegram widgets and API responses."""

    id: str
    title: str
    dod: str
    item_type: ItemType
    status: str
    confidence: float
    confidence_tier: ConfidenceTier
    energy_tier: EnergyTier
    effort: float
    day_anchor: DayAnchor | None = None
    scheduled_date: date | None = None
    due_at: datetime | None = None
    has_exact_time: bool = False
    deferral_count: int = 0


class CandidateItemDTO(BaseModel):
    """Medium-confidence candidate displayed for 1-tap triage."""

    id: str
    title: str
    dod: str
    confidence: float
    energy_tier: EnergyTier
    effort: float
    source_link: str | None = None


class DailyPlanView(BaseModel):
    """Complete Today view rendered in Telegram HTML widget, fallback, and API."""

    day: date
    budget: BudgetBreakdown
    effort_planned: float
    effort_done: float
    morning: list[TaskCardDTO] = Field(default_factory=list)
    afternoon: list[TaskCardDTO] = Field(default_factory=list)
    evening: list[TaskCardDTO] = Field(default_factory=list)
    candidates: list[CandidateItemDTO] = Field(default_factory=list)
    calendar_events: list[CalendarSlot] = Field(default_factory=list)
