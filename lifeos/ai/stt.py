"""Audio transcription service with gemini-3.5-transcribe and Hydrogen fallback."""

from __future__ import annotations

import asyncio
import base64
import inspect
from collections.abc import Callable
from typing import Any

from google import genai

from lifeos.ai.quota import HydrogenQuotaManager, QuotaExhaustedError
from lifeos.config import Settings
from lifeos.logging import get_logger

logger = get_logger(__name__)


async def _call_sdk(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    result = await asyncio.to_thread(fn, *args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


class AudioTranscriptionService:
    """Speech-to-text service using gemini-3.5-transcribe with gemini-4-hydrogen fallback."""

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

    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        mime_type: str = "audio/ogg",
        complex_audio: bool = False,
        language_codes: list[str] | None = None,
    ) -> str:
        """Transcribe inline audio bytes; falls back to gemini-4-hydrogen if needed."""
        if not audio_bytes:
            return ""

        b64 = base64.b64encode(audio_bytes).decode("ascii")
        audio_block: dict[str, Any] = {
            "type": "audio",
            "data": b64,
            "mime_type": mime_type,
        }

        if not complex_audio:
            kwargs: dict[str, Any] = {
                "model": self.settings.stt_model,
                "input": [audio_block],
            }
            if language_codes:
                kwargs["generation_config"] = {
                    "transcription_config": {"language_codes": language_codes}
                }
            try:
                response = await _call_sdk(self.client.interactions.create, **kwargs)
                text = (getattr(response, "output_text", None) or "").strip()
                if text:
                    return text
                logger.warning("Primary STT returned empty output; falling back to Hydrogen")
            except Exception as exc:
                logger.warning("Primary STT failed (%s); falling back to Hydrogen", exc)

        if not await self.quota_manager.consume(is_system=False):
            raise QuotaExhaustedError("Hydrogen daily quota exhausted for STT fallback")

        fallback_input: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "Transcribe this audio accurately with proper punctuation. "
                    "Return only the spoken transcript."
                ),
            },
            audio_block,
        ]
        response = await _call_sdk(
            self.client.interactions.create,
            model=self.settings.tier3_model,
            input=fallback_input,
        )
        return (getattr(response, "output_text", None) or "").strip()
