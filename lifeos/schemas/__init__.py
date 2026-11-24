"""Shared Pydantic schemas for LifeOS."""

from lifeos.schemas.ai import (
    ConfidenceTier,
    DayAnchor,
    EnergyTier,
    ExtractedItem,
    ItemType,
    SubtaskSpec,
    TriageResult,
    WeeklyAnalyticsOutput,
    WeeklyPlanOutput,
    WeeklyPlanSlot,
)
from lifeos.schemas.planner import (
    BudgetBreakdown,
    CalendarSlot,
    CandidateItemDTO,
    DailyPlanView,
    HealthSnapshot,
    TaskCardDTO,
)
from lifeos.schemas.shortcuts import (
    HealthMetricsPayload,
    NoteSyncItem,
    ReminderDiffAction,
    ReminderSyncItem,
    ShortcutsSyncRequest,
    ShortcutsSyncResponse,
    SiriCaptureRequest,
    SiriCaptureResponse,
)

__all__ = [
    "BudgetBreakdown",
    "CalendarSlot",
    "CandidateItemDTO",
    "ConfidenceTier",
    "DailyPlanView",
    "DayAnchor",
    "EnergyTier",
    "ExtractedItem",
    "HealthMetricsPayload",
    "HealthSnapshot",
    "ItemType",
    "NoteSyncItem",
    "ReminderDiffAction",
    "ReminderSyncItem",
    "ShortcutsSyncRequest",
    "ShortcutsSyncResponse",
    "SiriCaptureRequest",
    "SiriCaptureResponse",
    "SubtaskSpec",
    "TaskCardDTO",
    "TriageResult",
    "WeeklyAnalyticsOutput",
    "WeeklyPlanOutput",
    "WeeklyPlanSlot",
]
