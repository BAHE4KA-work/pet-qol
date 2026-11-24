"""ARQ worker settings, lifecycle hooks, and cron schedule for LifeOS."""

from __future__ import annotations

import importlib
import inspect
from datetime import UTC, tzinfo
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from arq import cron
from arq.connections import RedisSettings
from redis.asyncio import Redis

from lifeos.ai.decisions import DecisionTriageService
from lifeos.ai.embeddings import EmbeddingService
from lifeos.ai.quota import HydrogenQuotaManager
from lifeos.ai.stt import AudioTranscriptionService
from lifeos.ai.tier2 import Tier2Agent
from lifeos.ai.tier3 import Tier3HydrogenAgent
from lifeos.config import Settings, get_settings
from lifeos.db.base import create_engine_and_sessionmaker
from lifeos.integrations.caldav.client import ICloudCalDAVService
from lifeos.integrations.notion.client import NotionSyncService
from lifeos.integrations.telegram_bot.bot import LifeOSTelegramBot
from lifeos.logging import get_logger, setup_logging
from lifeos.worker.jobs import (
    daily_rollover_job,
    poll_gmail_job,
    process_raw_item,
    sync_caldav_job,
    sync_notion_job,
    weekly_plan_and_analytics_job,
)

logger = get_logger(__name__)


def _resolve_tz(tz_name: str) -> tzinfo:
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return UTC


def parse_redis_settings(redis_url: str) -> RedisSettings:
    """Parse a Redis URL into ARQ `RedisSettings`."""
    parsed = urlparse(redis_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 6379
    database = 0
    raw_path = (parsed.path or "").lstrip("/")
    if raw_path:
        try:
            database = int(raw_path.split("/", 1)[0])
        except ValueError:
            database = 0
    return RedisSettings(
        host=host,
        port=port,
        database=database,
        username=parsed.username,
        password=parsed.password,
        ssl=parsed.scheme == "rediss",
    )


async def on_startup(ctx: dict[str, Any]) -> None:
    """Initialize DB, Redis, quota manager, AI services, and optional integrations."""
    setup_logging()
    settings = ctx.get("settings")
    if not isinstance(settings, Settings):
        settings = get_settings()
        ctx["settings"] = settings

    if "session_factory" not in ctx:
        engine, session_factory = create_engine_and_sessionmaker(settings.database_url)
        ctx["engine"] = engine
        ctx["session_factory"] = session_factory
        ctx["_owns_engine"] = True
    else:
        ctx.setdefault("_owns_engine", False)

    if "redis_client" not in ctx:
        ctx["redis_client"] = Redis.from_url(settings.redis_url)
        ctx["_owns_redis"] = True
    else:
        ctx.setdefault("_owns_redis", False)

    if "quota_manager" not in ctx:
        ctx["quota_manager"] = HydrogenQuotaManager(
            redis_client=ctx["redis_client"],
            daily_limit=settings.hydrogen_daily_limit,
            reserved_system=settings.hydrogen_reserved_system,
        )

    quota_manager: HydrogenQuotaManager = ctx["quota_manager"]

    if settings.gemini_api_key:
        ctx.setdefault("embedding_service", EmbeddingService(settings))
        ctx.setdefault("decision_service", DecisionTriageService(settings))
        ctx.setdefault("stt_service", AudioTranscriptionService(settings, quota_manager))
        ctx.setdefault("tier2_agent", Tier2Agent(settings))
        ctx.setdefault("tier3_agent", Tier3HydrogenAgent(settings, quota_manager))
    else:
        logger.warning("GEMINI_API_KEY not configured; Gemini AI services disabled at startup")
        ctx.setdefault("embedding_service", None)
        ctx.setdefault("decision_service", None)
        ctx.setdefault("stt_service", None)
        ctx.setdefault("tier2_agent", None)
        ctx.setdefault("tier3_agent", None)

    if "notion_service" not in ctx:
        if not settings.notion_token:
            logger.warning("NOTION_TOKEN not configured; Notion sync disabled")
        ctx["notion_service"] = NotionSyncService(settings)

    if "caldav_service" not in ctx:
        if not (settings.caldav_username and settings.caldav_app_password):
            logger.warning("CalDAV credentials not configured; iCloud calendar sync disabled")
        ctx["caldav_service"] = ICloudCalDAVService(settings)

    if "telegram_bot" not in ctx:
        if settings.telegram_bot_token:
            ctx["telegram_bot"] = LifeOSTelegramBot(
                settings=settings,
                session_factory=ctx["session_factory"],
            )
            ctx["_owns_telegram_bot"] = True
        else:
            logger.warning("TELEGRAM_BOT_TOKEN not configured; Telegram widget refresh disabled")
            ctx["telegram_bot"] = None
            ctx["_owns_telegram_bot"] = False

    if "gmail_poller" not in ctx:
        if settings.gmail_credentials_json or settings.gmail_token_json:
            try:
                gmail_mod = importlib.import_module("lifeos.integrations.gmail.poller")
                poller_cls = gmail_mod.GmailReadOnlyPoller
                kwargs: dict[str, Any] = {"settings": settings}
                if "session_factory" in inspect.signature(poller_cls.__init__).parameters:
                    kwargs["session_factory"] = ctx["session_factory"]
                ctx["gmail_poller"] = poller_cls(**kwargs)
            except (ImportError, AttributeError) as exc:
                logger.warning("Gmail poller unavailable at startup: %s", exc)
                ctx["gmail_poller"] = None
        else:
            logger.warning("Gmail credentials not configured; Gmail polling disabled")
            ctx["gmail_poller"] = None


async def on_shutdown(ctx: dict[str, Any]) -> None:
    """Close owned Telegram bot HTTP session, Redis client, and DB engine."""
    telegram_bot = ctx.get("telegram_bot")
    if ctx.get("_owns_telegram_bot") and telegram_bot is not None:
        bot = getattr(telegram_bot, "bot", None)
        if bot is not None and getattr(bot, "session", None) is not None:
            await bot.session.close()

    redis_client = ctx.get("redis_client")
    if ctx.get("_owns_redis") and redis_client is not None:
        aclose = getattr(redis_client, "aclose", None)
        if callable(aclose):
            await aclose()
        else:
            await redis_client.close()

    engine = ctx.get("engine")
    if ctx.get("_owns_engine") and engine is not None:
        await engine.dispose()


_DEFAULT_SETTINGS = get_settings()


class WorkerSettings:
    """ARQ Worker configuration and periodic cron schedule for LifeOS."""

    redis_settings = parse_redis_settings(_DEFAULT_SETTINGS.redis_url)
    timezone = _resolve_tz(_DEFAULT_SETTINGS.timezone)
    functions = [
        process_raw_item,
        sync_notion_job,
        sync_caldav_job,
        poll_gmail_job,
        daily_rollover_job,
        weekly_plan_and_analytics_job,
    ]
    cron_jobs = [
        cron(sync_notion_job, minute={0, 15, 30, 45}),
        cron(sync_caldav_job, minute={0, 15, 30, 45}),
        cron(poll_gmail_job, minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}),
        cron(daily_rollover_job, hour=23, minute=50),
        cron(weekly_plan_and_analytics_job, weekday="sun", hour=20, minute=0),
    ]
    on_startup = on_startup
    on_shutdown = on_shutdown
