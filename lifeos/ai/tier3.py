"""Tier 3 reasoning agent backed by Gemini 4 Hydrogen (gemini-4-hydrogen)."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from datetime import date
from typing import Any, TypeVar

from google import genai
from pydantic import BaseModel

from lifeos.ai.quota import HydrogenQuotaManager, QuotaExhaustedError
from lifeos.config import Settings
from lifeos.schemas.ai import ExtractedItem, WeeklyAnalyticsOutput, WeeklyPlanOutput

SchemaT = TypeVar("SchemaT", bound=BaseModel)


async def _call_sdk(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    result = await asyncio.to_thread(fn, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _strip_json_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


class Tier3HydrogenAgent:
    """Deep reasoning, conflict resolution, weekly planning, and analytics agent."""

    def __init__(
        self,
        settings: Settings,
        quota_manager: HydrogenQuotaManager,
        client: Any = None,
    ) -> None:
        self.settings = settings
        self.quota_manager = quota_manager
        self.client = (
            client
            if client is not None
            else genai.Client(
                api_key=settings.gemini_api_key,
                http_options={"api_version": "v1beta"},
            )
        )

    async def _call_hydrogen(
        self,
        prompt: str,
        schema_model: type[SchemaT],
        *,
        is_system: bool,
        thinking_level: str = "high",
    ) -> SchemaT:
        if not await self.quota_manager.consume(is_system=is_system):
            raise QuotaExhaustedError("Gemini 4 Hydrogen daily quota exhausted")

        interaction = await _call_sdk(
            self.client.interactions.create,
            model=self.settings.tier3_model,
            input=prompt,
            generation_config={"thinking_level": thinking_level},
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": schema_model.model_json_schema(),
            },
        )
        raw = _strip_json_fences(getattr(interaction, "output_text", None) or "")
        return schema_model.model_validate_json(raw)

    async def deep_extract_and_resolve(
        self,
        raw_text: str,
        *,
        rag_context: list[str] | None = None,
        goals_context: list[dict[str, Any]] | None = None,
        calendar_slots: list[dict[str, Any]] | None = None,
        thinking_level: str = "high",
    ) -> ExtractedItem:
        """Perform deep extraction, duplicate/goal linking, and calendar conflict checks."""
        prompt = (
            "Deeply analyze this input, resolve duplicates against RAG context, align with "
            "active goals, check calendar_slots for schedule conflicts, and output an "
            "ExtractedItem JSON.\n"
            f"Active goals: {json.dumps(goals_context or [], default=str)}\n"
            f"RAG context: {json.dumps(rag_context or [], default=str)}\n"
            f"Calendar slots: {json.dumps(calendar_slots or [], default=str)}\n"
            f"Raw input:\n{raw_text}"
        )
        return await self._call_hydrogen(
            prompt,
            ExtractedItem,
            is_system=False,
            thinking_level=thinking_level,
        )

    async def build_weekly_plan(
        self,
        week_start: date,
        active_tasks: list[dict[str, Any]],
        goals: list[dict[str, Any]],
        calendar_slots: list[dict[str, Any]],
        thinking_level: str = "high",
    ) -> WeeklyPlanOutput:
        """Generate a balanced weekly schedule (uses reserved system quota)."""
        prompt = (
            f"Build a realistic weekly plan starting {week_start.isoformat()}.\n"
            f"Goals: {json.dumps(goals, default=str)}\n"
            f"Active tasks: {json.dumps(active_tasks, default=str)}\n"
            f"Calendar busy slots: {json.dumps(calendar_slots, default=str)}"
        )
        return await self._call_hydrogen(
            prompt,
            WeeklyPlanOutput,
            is_system=True,
            thinking_level=thinking_level,
        )

    async def generate_weekly_analytics(
        self,
        week_start: date,
        telemetry_rows: list[dict[str, Any]],
        stalled_tasks: list[dict[str, Any]],
        goals: list[dict[str, Any]],
        thinking_level: str = "high",
    ) -> WeeklyAnalyticsOutput:
        """Synthesize weekly telemetry, sleep correlation, and procrastination insights."""
        prompt = (
            f"Generate weekly analytics for week starting {week_start.isoformat()}.\n"
            f"Telemetry rows: {json.dumps(telemetry_rows, default=str)}\n"
            f"Stalled tasks: {json.dumps(stalled_tasks, default=str)}\n"
            f"Goals: {json.dumps(goals, default=str)}"
        )
        return await self._call_hydrogen(
            prompt,
            WeeklyAnalyticsOutput,
            is_system=True,
            thinking_level=thinking_level,
        )
