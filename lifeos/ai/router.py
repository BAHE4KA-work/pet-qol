"""4-tier AI pipeline router coordinating triage, STT, RAG embeddings, Tier 2, and Tier 3."""

from __future__ import annotations

from typing import Any

from lifeos.ai.decisions import DecisionTriageService
from lifeos.ai.embeddings import EmbeddingService
from lifeos.ai.quota import HydrogenQuotaManager, QuotaExhaustedError
from lifeos.ai.stt import AudioTranscriptionService
from lifeos.ai.tier2 import Tier2Agent
from lifeos.ai.tier3 import Tier3HydrogenAgent
from lifeos.config import Settings
from lifeos.db.models import RawItem
from lifeos.db.repositories import LifeOSRepository
from lifeos.logging import get_logger
from lifeos.schemas.ai import ExtractedItem, TriageResult

logger = get_logger(__name__)

DIRECT_SOURCES: frozenset[str] = frozenset({"siri", "telegram_bot", "notion"})
PASSIVE_SOURCES: frozenset[str] = frozenset({"telegram_userbot", "gmail", "apple_notes"})


class AIRouter:
    """Orchestrates incoming RawItem processing across the 4-tier Gemini/Gemma stack."""

    def __init__(
        self,
        settings: Settings,
        repo: LifeOSRepository,
        quota_manager: HydrogenQuotaManager,
        embedding_service: EmbeddingService,
        decision_service: DecisionTriageService,
        stt_service: AudioTranscriptionService,
        tier2_agent: Tier2Agent,
        tier3_agent: Tier3HydrogenAgent,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.quota_manager = quota_manager
        self.embedding_service = embedding_service
        self.decision_service = decision_service
        self.stt_service = stt_service
        self.tier2_agent = tier2_agent
        self.tier3_agent = tier3_agent

    async def process_raw_item(
        self,
        raw_item: RawItem,
        *,
        media_bytes: bytes | None = None,
        calendar_slots: list[dict[str, Any]] | None = None,
    ) -> tuple[TriageResult | None, ExtractedItem | None]:
        """Route a RawItem through triage, STT, RAG embedding, and Tier 2/3 extraction."""
        if raw_item.source in DIRECT_SOURCES:
            triage = TriageResult(
                is_actionable=1.0,
                confidence=1.0,
                item_type="task",
                energy_tier="medium",
                needs_hydrogen=0.0,
                urgency=3,
                cognitive_load=3,
                should_discard=False,
            )
        else:
            triage = await self.decision_service.triage(
                raw_item.raw_text,
                media_bytes=media_bytes,
                media_mime_type=raw_item.media_mime_type,
                modality=raw_item.modality,
                source=raw_item.source,
            )
            if triage.should_discard:
                return triage, None

        effective_text = (raw_item.raw_text or "").strip()
        if raw_item.modality == "audio" and media_bytes:
            transcript = await self.stt_service.transcribe(
                media_bytes,
                mime_type=raw_item.media_mime_type or "audio/ogg",
            )
            effective_text = (
                f"{effective_text}\n{transcript}".strip() if effective_text else transcript
            )

        if media_bytes is not None and raw_item.modality in {"image", "pdf", "document"}:
            embed_payload: str | bytes = media_bytes
            embed_modality = raw_item.modality
        elif effective_text:
            embed_payload = effective_text
            embed_modality = "text"
        elif media_bytes is not None:
            embed_payload = media_bytes
            embed_modality = raw_item.modality
        else:
            embed_payload = ""
            embed_modality = "text"

        emb_record = await self.embedding_service.embed_and_store(
            self.repo,
            entity_type="raw_item",
            entity_id=raw_item.id,
            text_or_bytes=embed_payload,
            modality=embed_modality,
            text_preview=effective_text[:500],
            mime_type=raw_item.media_mime_type,
        )

        rag_hits = await self.repo.semantic_search(
            emb_record.embedding,
            limit=5,
            entity_types=["task", "goal", "raw_item"],
        )
        rag_context: list[str] = []
        has_duplicate = False
        for rec, score in rag_hits:
            if rec.entity_type == "raw_item" and rec.entity_id == raw_item.id:
                continue
            rag_context.append(
                f"[{rec.entity_type}:{rec.entity_id}] (sim={score:.2f}) {rec.text_preview}"
            )
            if rec.entity_type == "task" and score >= 0.88:
                has_duplicate = True

        goals = await self.repo.list_active_goals()
        goals_context = [
            {
                "id": g.id,
                "title": g.title,
                "horizon": g.horizon,
                "priority": g.priority,
            }
            for g in goals
        ]

        has_conflict = has_duplicate or bool(calendar_slots and triage.item_type == "event")
        use_hydrogen = (
            triage.needs_hydrogen >= 0.65 or has_conflict
        ) and await self.quota_manager.can_consume(is_system=False)

        if use_hydrogen:
            try:
                extracted = await self.tier3_agent.deep_extract_and_resolve(
                    effective_text,
                    rag_context=rag_context,
                    goals_context=goals_context,
                    calendar_slots=calendar_slots,
                )
                return triage, extracted
            except QuotaExhaustedError:
                logger.warning("Hydrogen quota exhausted mid-routing; falling back to Tier 2")

        extracted = await self.tier2_agent.extract_and_decompose(
            effective_text,
            rag_context=rag_context,
            goals_context=goals_context,
            item_type_hint=triage.item_type,
        )
        return triage, extracted
