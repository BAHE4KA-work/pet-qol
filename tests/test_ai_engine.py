"""Comprehensive async tests for the 4-tier LifeOS AI engine."""

from __future__ import annotations

import base64
import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from google.genai import types
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from lifeos.ai import (
    AIRouter,
    AudioTranscriptionService,
    DecisionTriageService,
    EmbeddingService,
    HydrogenQuotaManager,
    QuotaExhaustedError,
    Tier2Agent,
    Tier3HydrogenAgent,
)
from lifeos.config import Settings
from lifeos.db.base import Base
from lifeos.db.repositories import LifeOSRepository, compute_sha256
from lifeos.schemas.ai import ExtractedItem, SubtaskSpec, WeeklyAnalyticsOutput, WeeklyPlanOutput


def _make_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "gemini_api_key": "test-gemini-key",
        "nvidia_nim_api_key": "test-nim-key",
        "nvidia_nim_base_url": "https://nim.test/v1",
        "nvidia_nim_model": "google/gemma-4-31b-it",
        "embedding_model": "gemini-embedding-2",
        "embedding_dim": 4,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


async def _make_repo() -> tuple[LifeOSRepository, AsyncSession, object]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    session = factory()
    return LifeOSRepository(session), session, engine


def _make_decisions_client(
    *,
    is_actionable: float = 0.9,
    item_type: str = "task",
    energy_tier: str = "medium",
    needs_hydrogen: float = 0.2,
    urgency_score: float = 2.0,
    load_score: float = 1.0,
) -> MagicMock:
    client = MagicMock()
    client.decisions.create = MagicMock(
        return_value=SimpleNamespace(
            answers={
                "is_actionable": SimpleNamespace(noul=is_actionable, confidence=is_actionable),
                "item_type": SimpleNamespace(choice=item_type, confidence=0.88),
                "energy_tier": SimpleNamespace(choice=energy_tier, confidence=0.82),
                "needs_hydrogen": SimpleNamespace(noul=needs_hydrogen, confidence=0.85),
                "urgency": SimpleNamespace(score=urgency_score, confidence=0.8),
                "cognitive_load": SimpleNamespace(score=load_score, confidence=0.8),
            }
        )
    )
    return client


# ---------------------------------------------------------------------------
# 1. Quota Manager
# ---------------------------------------------------------------------------
async def test_quota_manager_in_memory_caps() -> None:
    qm = HydrogenQuotaManager(redis_client=None, daily_limit=20, reserved_system=3)

    for _ in range(17):
        assert await qm.can_consume(is_system=False) is True
        assert await qm.consume(is_system=False) is True

    assert await qm.can_consume(is_system=False) is False
    assert await qm.consume(is_system=False) is False

    # 3 reserved system slots remain available
    for _ in range(3):
        assert await qm.can_consume(is_system=True) is True
        assert await qm.consume(is_system=True) is True

    assert await qm.can_consume(is_system=True) is False
    assert await qm.consume(is_system=True) is False

    rem = await qm.remaining(is_system=False)
    assert rem["used"] == 20
    assert rem["non_system_remaining"] == 0
    assert rem["system_remaining"] == 0


async def test_quota_manager_redis_key_and_caps() -> None:
    store: dict[str, int] = {}

    class FakeRedis:
        async def get(self, key: str) -> bytes | None:
            val = store.get(key)
            return str(val).encode("utf-8") if val is not None else None

        async def incr(self, key: str) -> int:
            store[key] = store.get(key, 0) + 1
            return store[key]

        async def decr(self, key: str) -> int:
            store[key] = store.get(key, 0) - 1
            return store[key]

        async def expire(self, key: str, ttl: int) -> bool:
            return True

    qm = HydrogenQuotaManager(redis_client=FakeRedis(), daily_limit=5, reserved_system=2)
    expected_key = f"lifeos:hydrogen:quota:{date.today().isoformat()}"

    assert await qm.consume(is_system=False) is True
    assert await qm.consume(is_system=False) is True
    assert await qm.consume(is_system=False) is True
    # 4th non-system rejected because cap is 5 - 2 = 3
    assert await qm.consume(is_system=False) is False
    assert store[expected_key] == 3

    # System can consume the remaining 2 slots up to 5
    assert await qm.consume(is_system=True) is True
    assert await qm.consume(is_system=True) is True
    assert await qm.consume(is_system=True) is False
    assert store[expected_key] == 5


# ---------------------------------------------------------------------------
# 2. Embedding Service
# ---------------------------------------------------------------------------
async def test_embedding_service_rejects_non_embedding_2() -> None:
    with pytest.raises(ValueError, match="gemini-embedding-2"):
        EmbeddingService(_make_settings(embedding_model="gemini-embedding-001"))

    with pytest.raises(ValueError, match="gemini-embedding-2"):
        EmbeddingService(_make_settings(embedding_model="text-embedding-004"))


async def test_embedding_service_multimodal_batch_query_and_dedup() -> None:
    mock_client = MagicMock()
    mock_client.models.embed_content = MagicMock(
        return_value=SimpleNamespace(
            embeddings=[SimpleNamespace(values=[0.1, 0.2, 0.3, 0.4])]
        )
    )
    svc = EmbeddingService(_make_settings(), client=mock_client)

    vec_text = await svc.embed_text("Buy milk")
    assert vec_text == [0.1, 0.2, 0.3, 0.4]
    call_kw = mock_client.models.embed_content.call_args.kwargs
    assert call_kw["model"] == "gemini-embedding-2"
    assert isinstance(call_kw["config"], types.EmbedContentConfig)
    assert call_kw["config"].output_dimensionality == 4

    vec_query = await svc.embed_query("milk errands")
    assert vec_query == [0.1, 0.2, 0.3, 0.4]
    assert "task: search result | query: milk errands" in str(
        mock_client.models.embed_content.call_args.kwargs["contents"]
    )

    for mime, payload in [
        ("image/png", b"png-bytes"),
        ("audio/mp3", b"mp3-bytes"),
        ("application/pdf", b"pdf-bytes"),
    ]:
        await svc.embed_bytes(payload, mime)
        sent = mock_client.models.embed_content.call_args.kwargs["contents"]
        assert isinstance(sent[0], types.Part)
        assert sent[0].inline_data.mime_type == mime

    mock_client.models.embed_content.return_value = SimpleNamespace(
        embeddings=[
            SimpleNamespace(values=[1.0, 0.0, 0.0, 0.0]),
            SimpleNamespace(values=[0.0, 1.0, 0.0, 0.0]),
        ]
    )
    batch = await svc.embed_batch(["hello", (b"img", "image/jpeg")])
    assert len(batch) == 2

    repo, session, engine = await _make_repo()
    try:
        mock_client.models.embed_content.reset_mock()
        mock_client.models.embed_content.return_value = SimpleNamespace(
            embeddings=[SimpleNamespace(values=[0.5, 0.5, 0.5, 0.5])]
        )
        rec1 = await svc.embed_and_store(repo, "task", "t1", "Finish report")
        assert rec1.content_hash == compute_sha256("Finish report")
        assert mock_client.models.embed_content.call_count == 1

        # Second call with identical content skips Gemini API
        rec2 = await svc.embed_and_store(repo, "task", "t2", "Finish report")
        assert rec2.id == rec1.id
        assert mock_client.models.embed_content.call_count == 1
    finally:
        await session.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# 3. Decision Triage Service
# ---------------------------------------------------------------------------
async def test_decision_triage_single_pass_and_multimodal() -> None:
    client = _make_decisions_client(
        is_actionable=0.82,
        item_type="event",
        energy_tier="deep",
        needs_hydrogen=0.71,
        urgency_score=3.6,
        load_score=0.0,
    )
    svc = DecisionTriageService(_make_settings(), client=client)

    res = await svc.triage(
        "Review Q4 architecture deck",
        media_bytes=b"pdfdata",
        media_mime_type="application/pdf",
        modality="pdf",
        source="gmail",
    )

    assert client.decisions.create.call_count == 1
    kwargs = client.decisions.create.call_args.kwargs
    assert kwargs["model"] == "gemini-decision-1"
    assert set(kwargs["questions"].keys()) == {
        "is_actionable",
        "item_type",
        "energy_tier",
        "needs_hydrogen",
        "urgency",
        "cognitive_load",
    }
    assert kwargs["questions"]["item_type"]["type"] == "choice"
    assert set(kwargs["questions"]["item_type"]["criteria"].keys()) == {
        "event",
        "task",
        "goal_idea",
        "noise",
    }
    assert isinstance(kwargs["input"], list)
    assert kwargs["input"][0]["type"] == "document"
    assert kwargs["input"][0]["data"] == base64.b64encode(b"pdfdata").decode("ascii")

    assert res.item_type == "event"
    assert res.energy_tier == "deep"
    assert res.urgency == 5  # round(3.6) + 1 = 5
    assert res.cognitive_load == 1  # round(0.0) + 1 = 1
    assert res.confidence == pytest.approx(0.82)
    assert res.should_discard is False

    # Noise or low actionability sets should_discard=True
    noise_client = _make_decisions_client(is_actionable=0.6, item_type="noise")
    noise_svc = DecisionTriageService(_make_settings(), client=noise_client)
    assert (await noise_svc.triage("50% off sale!")).should_discard is True

    low_client = _make_decisions_client(is_actionable=0.10, item_type="task")
    low_svc = DecisionTriageService(_make_settings(), client=low_client)
    assert (await low_svc.triage("hmm")).should_discard is True


# ---------------------------------------------------------------------------
# 4. Audio Transcription Service (STT)
# ---------------------------------------------------------------------------
async def test_stt_primary_and_hydrogen_fallback() -> None:
    qm = HydrogenQuotaManager(daily_limit=3, reserved_system=1)
    client = MagicMock()
    client.interactions.create = MagicMock(
        return_value=SimpleNamespace(output_text="Call Alex at 4pm")
    )
    stt = AudioTranscriptionService(_make_settings(), qm, client=client)

    transcript = await stt.transcribe(b"audio1", mime_type="audio/ogg")
    assert transcript == "Call Alex at 4pm"
    assert client.interactions.create.call_args.kwargs["model"] == "gemini-3.5-transcribe"
    assert (await qm.remaining())["used"] == 0

    # complex_audio=True routes to gemini-4-hydrogen and consumes 1 non-system quota
    client.interactions.create.return_value = SimpleNamespace(output_text="Noisy transcript")
    t2 = await stt.transcribe(b"audio2", complex_audio=True)
    assert t2 == "Noisy transcript"
    assert client.interactions.create.call_args.kwargs["model"] == "gemini-4-hydrogen"
    assert (await qm.remaining())["used"] == 1

    # Primary failure triggers fallback and consumes 2nd (final) non-system quota
    client.interactions.create.side_effect = [
        RuntimeError("STT outage"),
        SimpleNamespace(output_text="Recovered transcript"),
    ]
    t3 = await stt.transcribe(b"audio3")
    assert t3 == "Recovered transcript"
    assert (await qm.remaining())["used"] == 2

    # Next fallback raises QuotaExhaustedError because non-system limit (3 - 1 = 2) is reached
    client.interactions.create.side_effect = RuntimeError("STT outage")
    with pytest.raises(QuotaExhaustedError):
        await stt.transcribe(b"audio4")


# ---------------------------------------------------------------------------
# 5. Tier 2 Agent + NVIDIA NIM Fallback
# ---------------------------------------------------------------------------
async def test_tier2_primary_and_nim_fallback() -> None:
    extracted_payload = ExtractedItem(
        title="Prepare quarterly slides",
        dod="Export PDF deck and share in Slack",
        item_type="task",
        energy_tier="deep",
    ).model_dump_json()

    client = MagicMock()
    client.interactions.create = MagicMock(
        return_value=SimpleNamespace(output_text=extracted_payload)
    )
    agent = Tier2Agent(_make_settings(), client=client)

    item = await agent.extract_and_decompose("Make Q4 slides")
    assert item.title == "Prepare quarterly slides"
    assert client.interactions.create.call_args.kwargs["model"] == "gemma-4-31b-it"
    assert (
        client.interactions.create.call_args.kwargs["response_format"]["mime_type"]
        == "application/json"
    )

    # Simulate HTTP 429 from primary -> automatic NVIDIA NIM fallback via httpx
    def _raise_429(**kwargs: object) -> None:
        req = httpx.Request("POST", "https://generativelanguage.googleapis.com")
        resp = httpx.Response(429, request=req)
        raise httpx.HTTPStatusError("Rate limited", request=req, response=resp)

    client.interactions.create = MagicMock(side_effect=_raise_429)

    nim_subtasks = {
        "subtasks": [
            SubtaskSpec(title="Outline 3 key slides", dod="Bullet outline saved").model_dump(),
            SubtaskSpec(title="Add metrics chart", dod="Chart pasted on slide 2").model_dump(),
        ]
    }
    captured_nim_req: dict[str, object] = {}

    def _nim_handler(request: httpx.Request) -> httpx.Response:
        captured_nim_req["url"] = str(request.url)
        captured_nim_req["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(nim_subtasks)}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_nim_handler)) as hc:
        nim_agent = Tier2Agent(_make_settings(), client=client, http_client=hc)
        subtasks = await nim_agent.split_stalled_task("Deck", "PDF shared", 4)
        assert len(subtasks) == 2
        assert subtasks[0].title == "Outline 3 key slides"
        assert captured_nim_req["url"] == "https://nim.test/v1/chat/completions"
        assert captured_nim_req["body"]["response_format"] == {"type": "json_object"}  # type: ignore[index]

        # Also test rebalance_day over NIM fallback
        rebalance_body = {
            "decisions": [
                {"task_id": "t1", "action": "keep", "new_anchor": "morning", "reason": "High ROI"},
                {"task_id": "t2", "action": "defer", "new_anchor": None, "reason": "Over budget"},
            ]
        }

        def _rebalance_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": json.dumps(rebalance_body)}}]},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(_rebalance_handler)) as hc2:
            nim_agent2 = Tier2Agent(_make_settings(), client=client, http_client=hc2)
            decisions = await nim_agent2.rebalance_day(
                [{"id": "t1", "effort": 6.0}, {"id": "t2", "effort": 18.0}],
                daily_budget=15.0,
            )
            assert len(decisions) == 2
            assert decisions[1]["action"] == "defer"


# ---------------------------------------------------------------------------
# 6. Tier 3 Hydrogen Agent
# ---------------------------------------------------------------------------
async def test_tier3_hydrogen_quota_and_methods() -> None:
    qm = HydrogenQuotaManager(daily_limit=4, reserved_system=1)
    client = MagicMock()

    extracted_json = ExtractedItem(
        title="Reschedule sync",
        dod="Calendar invite updated",
        item_type="event",
        has_schedule_conflict=True,
    ).model_dump_json()
    plan_json = WeeklyPlanOutput(
        week_start=date(2026, 11, 23),
        focus_theme="Ship MVP",
    ).model_dump_json()
    analytics_json = WeeklyAnalyticsOutput(
        week_start=date(2026, 11, 23),
        summary_markdown="Strong focus on deep mornings.",
        sleep_productivity_corr=0.68,
    ).model_dump_json()

    client.interactions.create = MagicMock(
        side_effect=[
            SimpleNamespace(output_text=extracted_json),
            SimpleNamespace(output_text=plan_json),
            SimpleNamespace(output_text=analytics_json),
        ]
    )
    t3 = Tier3HydrogenAgent(_make_settings(), qm, client=client)

    res = await t3.deep_extract_and_resolve("Move sync", thinking_level="xhigh")
    assert res.has_schedule_conflict is True
    assert client.interactions.create.call_args.kwargs["generation_config"] == {
        "thinking_level": "xhigh"
    }

    # Burn remaining 2 non-system slots -> non-system exhausted (3/3), 1 system slot left
    assert await qm.consume(is_system=False) is True
    assert await qm.consume(is_system=False) is True
    with pytest.raises(QuotaExhaustedError):
        await t3.deep_extract_and_resolve("Should fail before API call")

    # System call STILL succeeds using reserved slot (4/4)
    plan = await t3.build_weekly_plan(date(2026, 11, 23), [], [], [])
    assert plan.focus_theme == "Ship MVP"

    # Now system quota is also exhausted (4/4)
    with pytest.raises(QuotaExhaustedError):
        await t3.generate_weekly_analytics(date(2026, 11, 23), [], [], [])


# ---------------------------------------------------------------------------
# 7. AI Router end-to-end orchestration
# ---------------------------------------------------------------------------
async def test_ai_router_passive_noise_and_direct_and_hydrogen_fallback() -> None:
    repo, session, engine = await _make_repo()
    try:
        settings = _make_settings()

        # Seed a goal and an existing task embedding for RAG / duplicate detection
        await repo.upsert_goal(title="Launch LifeOS")
        existing_task = await repo.create_task(
            title="Renew passport",
            dod="Submit renewal application",
        )
        await repo.upsert_embedding(
            entity_type="task",
            entity_id=existing_task.id,
            content_hash=compute_sha256("Renew passport"),
            embedding=[1.0, 0.0, 0.0, 0.0],
            text_preview="Renew passport",
        )

        # Case A: passive noise discards without calling STT or LLM
        raw_noise, _ = await repo.create_raw_item_if_new(
            source="gmail",
            content_hash=compute_sha256("spam"),
            modality="audio",
            raw_text="Weekly coupon newsletter",
        )
        qm = HydrogenQuotaManager(daily_limit=20, reserved_system=3)
        emb_client = MagicMock()
        emb_client.models.embed_content = MagicMock(
            return_value=SimpleNamespace(
                embeddings=[SimpleNamespace(values=[0.0, 1.0, 0.0, 0.0])]
            )
        )
        emb_svc = EmbeddingService(settings, client=emb_client)
        dec_svc = DecisionTriageService(
            settings, client=_make_decisions_client(is_actionable=0.05, item_type="noise")
        )
        stt_svc = AudioTranscriptionService(settings, qm, client=MagicMock())
        stt_svc.transcribe = AsyncMock(return_value="should not run")  # type: ignore[method-assign]
        t2_agent = Tier2Agent(settings, client=MagicMock())
        t2_agent.extract_and_decompose = AsyncMock()  # type: ignore[method-assign]
        t3_agent = Tier3HydrogenAgent(settings, qm, client=MagicMock())
        t3_agent.deep_extract_and_resolve = AsyncMock()  # type: ignore[method-assign]

        router = AIRouter(
            settings, repo, qm, emb_svc, dec_svc, stt_svc, t2_agent, t3_agent
        )
        triage_res, extracted = await router.process_raw_item(
            raw_noise, media_bytes=b"voice-bytes"
        )
        assert triage_res is not None and triage_res.should_discard is True
        assert extracted is None
        stt_svc.transcribe.assert_not_called()
        t2_agent.extract_and_decompose.assert_not_called()
        t3_agent.deep_extract_and_resolve.assert_not_called()

        # Case B: direct source ("siri") bypasses discard, gets confidence=1.0, runs STT -> Tier 2
        raw_siri, _ = await repo.create_raw_item_if_new(
            source="siri",
            content_hash=compute_sha256("siri-voice"),
            modality="audio",
            media_mime_type="audio/m4a",
        )
        dec_svc.triage = AsyncMock()  # type: ignore[method-assign]
        stt_svc.transcribe = AsyncMock(return_value="Buy coffee beans")  # type: ignore[method-assign]
        t2_agent.extract_and_decompose = AsyncMock(  # type: ignore[method-assign]
            return_value=ExtractedItem(title="Buy coffee beans", dod="Beans purchased")
        )
        triage_b, ext_b = await router.process_raw_item(raw_siri, media_bytes=b"m4a-bytes")
        assert triage_b is not None
        assert triage_b.confidence == 1.0
        assert triage_b.should_discard is False
        assert ext_b is not None and ext_b.title == "Buy coffee beans"
        dec_svc.triage.assert_not_called()
        stt_svc.transcribe.assert_awaited_once()
        t2_agent.extract_and_decompose.assert_awaited_once()

        # Case C: high needs_hydrogen routes to Tier 3, and falls back to Tier 2 on QuotaExhausted
        raw_complex, _ = await repo.create_raw_item_if_new(
            source="apple_notes",
            content_hash=compute_sha256("complex-note"),
            modality="text",
            raw_text="Plan quarterly OKRs with team",
        )
        dec_svc.triage = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(
                is_actionable=0.95,
                confidence=0.95,
                item_type="goal_idea",
                energy_tier="deep",
                needs_hydrogen=0.85,
                urgency=4,
                cognitive_load=5,
                should_discard=False,
            )
        )
        t3_agent.deep_extract_and_resolve = AsyncMock(  # type: ignore[method-assign]
            return_value=ExtractedItem(title="Plan Q4 OKRs", dod="OKRs drafted")
        )
        t2_agent.extract_and_decompose.reset_mock()
        _, ext_c = await router.process_raw_item(raw_complex)
        assert ext_c is not None and ext_c.title == "Plan Q4 OKRs"
        t3_agent.deep_extract_and_resolve.assert_awaited_once()
        t2_agent.extract_and_decompose.assert_not_called()

        # Now simulate QuotaExhaustedError in Tier 3 -> fallback to Tier 2
        raw_complex2, _ = await repo.create_raw_item_if_new(
            source="apple_notes",
            content_hash=compute_sha256("complex-note-2"),
            modality="text",
            raw_text="Plan offsite agenda",
        )
        t3_agent.deep_extract_and_resolve = AsyncMock(  # type: ignore[method-assign]
            side_effect=QuotaExhaustedError("exhausted")
        )
        t2_agent.extract_and_decompose = AsyncMock(  # type: ignore[method-assign]
            return_value=ExtractedItem(title="Plan offsite agenda", dod="Agenda shared")
        )
        _, ext_d = await router.process_raw_item(raw_complex2)
        assert ext_d is not None and ext_d.title == "Plan offsite agenda"
        t2_agent.extract_and_decompose.assert_awaited_once()
    finally:
        await session.close()
        await engine.dispose()
