"""Tier 2 worker agent using gemma-4-31b-it with NVIDIA NIM HTTP fallback."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from typing import Any

import httpx
from google import genai
from google.genai import errors
from pydantic import BaseModel

from lifeos.config import Settings
from lifeos.logging import get_logger
from lifeos.schemas.ai import ExtractedItem, SubtaskSpec

logger = get_logger(__name__)


class _SubtaskListOutput(BaseModel):
    subtasks: list[SubtaskSpec]


class _RebalanceDecision(BaseModel):
    task_id: str
    action: str
    new_anchor: str | None = None
    reason: str = ""


class _RebalanceOutput(BaseModel):
    decisions: list[_RebalanceDecision]


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


def _is_fallback_error(exc: Exception) -> bool:
    if isinstance(
        exc,
        errors.APIError | httpx.HTTPError | TimeoutError | ConnectionError | OSError,
    ):
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    return False


class Tier2Agent:
    """Standard extraction, decomposition, and rebalancing agent (gemma-4-31b-it)."""

    def __init__(
        self,
        settings: Settings,
        client: Any = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.client = (
            client
            if client is not None
            else genai.Client(
                api_key=settings.gemini_api_key,
                http_options={"api_version": "v1beta"},
            )
        )
        self._http_client = http_client

    async def _call_nim(self, prompt: str, schema: dict[str, Any]) -> str:
        url = f"{self.settings.nvidia_nim_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.nvidia_nim_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.settings.nvidia_nim_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return only valid JSON strictly conforming to this JSON Schema: "
                        f"{json.dumps(schema)}"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }

        if self._http_client is not None:
            resp = await self._http_client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        else:
            async with httpx.AsyncClient(timeout=60.0) as hc:
                resp = await hc.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()

        return str(data["choices"][0]["message"]["content"])

    async def _generate_structured(self, prompt: str, schema_model: type[BaseModel]) -> str:
        schema = schema_model.model_json_schema()
        try:
            interaction = await _call_sdk(
                self.client.interactions.create,
                model=self.settings.tier2_model,
                input=prompt,
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": schema,
                },
            )
            raw = getattr(interaction, "output_text", None) or ""
            if not raw.strip():
                raise ValueError("Empty response from Tier 2 Gemini interaction")
            return _strip_json_fences(raw)
        except Exception as exc:
            if not _is_fallback_error(exc):
                raise
            logger.warning("Tier 2 primary failed (%s); falling back to NVIDIA NIM", exc)
            nim_raw = await self._call_nim(prompt, schema)
            return _strip_json_fences(nim_raw)

    async def extract_and_decompose(
        self,
        raw_text: str,
        *,
        rag_context: list[str] | None = None,
        goals_context: list[dict[str, Any]] | None = None,
        item_type_hint: str = "task",
    ) -> ExtractedItem:
        """Extract a normalized actionable item and optional subtasks from raw text."""
        prompt = (
            "Extract a structured actionable LifeOS item with a clear Definition of Done (dod).\n"
            f"Suggested item_type: {item_type_hint}\n"
            f"Active goals: {json.dumps(goals_context or [], default=str)}\n"
            f"Related RAG context: {json.dumps(rag_context or [], default=str)}\n"
            f"Raw input:\n{raw_text}"
        )
        raw_json = await self._generate_structured(prompt, ExtractedItem)
        return ExtractedItem.model_validate_json(raw_json)

    async def split_stalled_task(
        self,
        title: str,
        dod: str,
        deferral_count: int,
    ) -> list[SubtaskSpec]:
        """Break a repeatedly deferred task into 2-4 low-friction micro-steps."""
        prompt = (
            "This task has been deferred multiple times. Split it into 2-4 small, concrete "
            "low-friction micro-steps (each with a clear dod).\n"
            f"Title: {title}\n"
            f"Definition of Done: {dod}\n"
            f"Deferral count: {deferral_count}\n"
            'Return JSON object with key "subtasks".'
        )
        raw_json = await self._generate_structured(prompt, _SubtaskListOutput)
        parsed = json.loads(raw_json)
        if isinstance(parsed, list):
            return [SubtaskSpec.model_validate(item) for item in parsed]
        return _SubtaskListOutput.model_validate(parsed).subtasks

    async def rebalance_day(
        self,
        tasks_summary: list[dict[str, Any]],
        daily_budget: float,
    ) -> list[dict[str, Any]]:
        """Recommend keep/defer/move_anchor actions when planned effort exceeds budget."""
        prompt = (
            f"Daily effort budget is {daily_budget:.2f}. Review these tasks and return "
            'a JSON object with key "decisions", a list of {task_id, action, new_anchor, reason} '
            'where action is one of "keep", "defer", or "move_anchor".\n'
            f"Tasks: {json.dumps(tasks_summary, default=str)}"
        )
        raw_json = await self._generate_structured(prompt, _RebalanceOutput)
        parsed = json.loads(raw_json)
        if isinstance(parsed, list):
            return [dict(item) for item in parsed]
        output = _RebalanceOutput.model_validate(parsed)
        return [item.model_dump() for item in output.decisions]
