"""Tier 1 single-pass triage using Gemini Decision 1 (gemini-decision-1)."""

from __future__ import annotations

import asyncio
import base64
import inspect
from collections.abc import Callable
from typing import Any, cast

from google import genai

from lifeos.config import Settings
from lifeos.schemas.ai import EnergyTier, ItemType, TriageResult

_VALID_ITEM_TYPES: frozenset[str] = frozenset({"event", "task", "goal_idea", "noise"})
_VALID_ENERGY_TIERS: frozenset[str] = frozenset({"deep", "medium", "low"})


async def _call_sdk(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    result = await asyncio.to_thread(fn, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _get_field(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj[key]
    return getattr(obj, key)


def _resolve_block_type(modality: str, mime_type: str | None) -> str:
    if modality == "audio" or (mime_type and mime_type.startswith("audio/")):
        return "audio"
    if modality == "image" or (mime_type and mime_type.startswith("image/")):
        return "image"
    return "document"


def _build_questions() -> dict[str, dict[str, Any]]:
    return {
        "is_actionable": {
            "type": "noul",
            "instructions": (
                "Does this input contain an actionable commitment, task, calendar event, "
                "or meaningful goal idea for the user?"
            ),
            "criteria": {
                "true_description": "Contains a task, deadline, meeting, or concrete goal.",
                "false_description": "Newsletter, receipt, promo, spam, or idle chatter.",
            },
        },
        "item_type": {
            "type": "choice",
            "instructions": "Classify the primary type of this incoming item.",
            "criteria": {
                "event": "Scheduled meeting, call, or appointment with a date or time.",
                "task": "Concrete action item, deliverable, errand, or follow-up.",
                "goal_idea": "Longer-term project idea, strategic reflection, or goal.",
                "noise": "Marketing, automated notification, spam, or non-actionable chatter.",
            },
        },
        "energy_tier": {
            "type": "choice",
            "instructions": "Estimate the cognitive energy tier required to act on this item.",
            "criteria": {
                "deep": "Requires sustained deep focus, design, coding, writing, or analysis.",
                "medium": (
                    "Moderate planning, structured communication, or multi-step coordination."
                ),
                "low": "Quick reply, simple logistics, mechanical errand, or under 10 minutes.",
            },
        },
        "needs_hydrogen": {
            "type": "noul",
            "instructions": (
                "Does this item require deep reasoning, ambiguous calendar conflict "
                "resolution, or multi-goal strategic synthesis?"
            ),
            "criteria": {
                "true_description": "Ambiguous, complex, multi-party scheduling, or high-stakes.",
                "false_description": "Clear single task or obvious noise handled by fast triage.",
            },
        },
        "urgency": {
            "type": "score",
            "instructions": "Rate the time sensitivity and urgency of this item.",
            "criteria": [
                "No time sensitivity or purely informational",
                "Low urgency, someday or flexible timeline",
                "Moderate urgency, within the next week",
                "High urgency, needed within 24-48 hours",
                "Critical or immediate action required today",
            ],
        },
        "cognitive_load": {
            "type": "score",
            "instructions": "Rate the mental effort required to complete or resolve this item.",
            "criteria": [
                "Trivial mechanical step",
                "Light administrative effort",
                "Standard focused work",
                "Heavy analytical or creative work",
                "Extreme deep focus or complex synthesis",
            ],
        },
    }


class DecisionTriageService:
    """Single-pass calibrated triage service backed by gemini-decision-1."""

    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.settings = settings
        self.client = (
            client
            if client is not None
            else genai.Client(
                api_key=settings.gemini_api_key,
                http_options={"api_version": "v1beta"},
            )
        )

    @staticmethod
    def _build_input(
        text: str | None,
        *,
        media_bytes: bytes | None = None,
        media_mime_type: str | None = None,
        modality: str = "text",
        source: str | None = None,
    ) -> str | list[dict[str, Any]]:
        prefix = f"[source: {source}]\n" if source else ""
        if media_bytes is not None:
            b64 = base64.b64encode(media_bytes).decode("ascii")
            block_type = _resolve_block_type(modality, media_mime_type)
            default_mime = {
                "audio": "audio/ogg",
                "image": "image/jpeg",
                "document": "application/pdf",
            }[block_type]
            blocks: list[dict[str, Any]] = [
                {
                    "type": block_type,
                    "data": b64,
                    "mime_type": media_mime_type or default_mime,
                }
            ]
            if text:
                blocks.append({"type": "text", "text": f"{prefix}{text}"})
            elif prefix:
                blocks.append({"type": "text", "text": prefix.strip()})
            return blocks

        if text and text.strip():
            return f"{prefix}{text}"
        raise ValueError("Either text or media_bytes must be provided for triage")

    async def triage(
        self,
        text: str | None = None,
        *,
        media_bytes: bytes | None = None,
        media_mime_type: str | None = None,
        modality: str = "text",
        source: str | None = None,
    ) -> TriageResult:
        """Run single-pass 6-question calibrated triage and return TriageResult."""
        payload = self._build_input(
            text,
            media_bytes=media_bytes,
            media_mime_type=media_mime_type,
            modality=modality,
            source=source,
        )
        result = await _call_sdk(
            self.client.decisions.create,
            model=self.settings.decision_model,
            input=payload,
            questions=_build_questions(),
        )
        answers = _get_field(result, "answers")

        is_actionable = min(1.0, max(0.0, float(_get_field(answers["is_actionable"], "noul"))))
        raw_item_type = str(_get_field(answers["item_type"], "choice"))
        item_type = cast(
            ItemType, raw_item_type if raw_item_type in _VALID_ITEM_TYPES else "task"
        )
        raw_energy = str(_get_field(answers["energy_tier"], "choice"))
        energy_tier = cast(
            EnergyTier, raw_energy if raw_energy in _VALID_ENERGY_TIERS else "medium"
        )
        needs_hydrogen = min(
            1.0, max(0.0, float(_get_field(answers["needs_hydrogen"], "noul")))
        )

        urgency_score = float(_get_field(answers["urgency"], "score"))
        load_score = float(_get_field(answers["cognitive_load"], "score"))
        urgency = min(5, max(1, round(urgency_score) + 1))
        cognitive_load = min(5, max(1, round(load_score) + 1))

        should_discard = item_type == "noise" or is_actionable < 0.15
        return TriageResult(
            is_actionable=is_actionable,
            confidence=is_actionable,
            item_type=item_type,
            energy_tier=energy_tier,
            needs_hydrogen=needs_hydrogen,
            urgency=urgency,
            cognitive_load=cognitive_load,
            should_discard=should_discard,
        )
