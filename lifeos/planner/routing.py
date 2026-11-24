"""Confidence tier classification and item routing rules."""

from __future__ import annotations

from typing import Any

from lifeos.config import Settings, get_settings
from lifeos.schemas.ai import ConfidenceTier, ExtractedItem

DIRECT_SOURCES: set[str] = {"siri", "telegram_bot", "notion"}

_STATUS_BY_TIER: dict[ConfidenceTier, str] = {
    "high": "active",
    "medium": "candidate",
    "low": "someday",
}


def classify_confidence_tier(
    confidence: float,
    source: str,
    settings: Settings | None = None,
) -> ConfidenceTier:
    """Classify an item into high, medium, or low confidence tier."""
    cfg = settings or get_settings()
    if source in DIRECT_SOURCES or confidence >= cfg.high_confidence_threshold:
        return "high"
    if confidence >= cfg.medium_confidence_threshold:
        return "medium"
    return "low"


def route_extracted_item(
    extracted: ExtractedItem,
    confidence: float,
    source: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Determine target task status and downstream sync flags for an extracted item."""
    tier = classify_confidence_tier(confidence=confidence, source=source, settings=settings)
    return {
        "status": _STATUS_BY_TIER[tier],
        "confidence_tier": tier,
        "push_to_reminders": tier == "high" and extracted.item_type == "task",
        "push_to_calendar": tier == "high" and extracted.item_type == "event",
        "show_in_widget_candidates": tier == "medium",
    }
