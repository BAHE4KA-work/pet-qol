"""FastAPI application factory for LifeOS."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from lifeos.api.routes_health import router as health_router
from lifeos.api.routes_planner import router as planner_router
from lifeos.api.routes_shortcuts import router as shortcuts_router
from lifeos.config import Settings, get_settings
from lifeos.db.base import create_engine_and_sessionmaker
from lifeos.logging import get_logger, setup_logging

logger = get_logger(__name__)


def create_app(
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    enqueue_fn: Callable[[str], Any] | None = None,
    ai_router: Any = None,
) -> FastAPI:
    """Create and configure the LifeOS FastAPI application."""
    setup_logging()
    resolved_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        created_engine: AsyncEngine | None = None
        if getattr(application.state, "session_factory", None) is None:
            created_engine, created_factory = create_engine_and_sessionmaker(
                resolved_settings.database_url
            )
            application.state.session_factory = created_factory
            application.state.engine = created_engine
            logger.info("Initialized database engine for FastAPI lifespan")
        try:
            yield
        finally:
            if created_engine is not None:
                await created_engine.dispose()
                logger.info("Disposed database engine on FastAPI shutdown")

    app = FastAPI(
        title="LifeOS API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.session_factory = session_factory
    app.state.enqueue_fn = enqueue_fn
    app.state.ai_router = ai_router

    if settings is not None:
        app.dependency_overrides[get_settings] = lambda: resolved_settings

    app.include_router(health_router)
    app.include_router(shortcuts_router)
    app.include_router(planner_router)
    return app
