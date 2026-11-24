"""Shared pytest fixtures for LifeOS async DB, Redis, and settings tests."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lifeos.config import Settings
from lifeos.db.base import Base
from lifeos.db.repositories import LifeOSRepository


class FakeRedis:
    """Lightweight async in-memory Redis stub supporting get, incr, decr, expire, and ttl."""

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._expires_at: dict[str, float] = {}

    def _purge_if_expired(self, key: str) -> None:
        exp = self._expires_at.get(key)
        if exp is not None and time.monotonic() >= exp:
            self._data.pop(key, None)
            self._expires_at.pop(key, None)

    async def get(self, key: str) -> bytes | None:
        """Return encoded string value for key, or None if missing/expired."""
        self._purge_if_expired(key)
        value = self._data.get(key)
        return value.encode("utf-8") if value is not None else None

    async def incr(self, key: str) -> int:
        """Increment integer value at key by 1, initializing to 0 if absent."""
        self._purge_if_expired(key)
        current = int(self._data.get(key, "0"))
        updated = current + 1
        self._data[key] = str(updated)
        return updated

    async def decr(self, key: str) -> int:
        """Decrement integer value at key by 1, initializing to 0 if absent."""
        self._purge_if_expired(key)
        current = int(self._data.get(key, "0"))
        updated = current - 1
        self._data[key] = str(updated)
        return updated

    async def expire(self, key: str, seconds: int) -> bool:
        """Set key expiration in seconds; returns True if key exists."""
        self._purge_if_expired(key)
        if key not in self._data:
            return False
        if seconds <= 0:
            self._data.pop(key, None)
            self._expires_at.pop(key, None)
            return True
        self._expires_at[key] = time.monotonic() + seconds
        return True

    async def ttl(self, key: str) -> int:
        """Return remaining TTL in seconds, -1 if persistent, or -2 if missing."""
        self._purge_if_expired(key)
        if key not in self._data:
            return -2
        exp = self._expires_at.get(key)
        if exp is None:
            return -1
        return max(0, int(round(exp - time.monotonic())))


@pytest.fixture
def test_settings() -> Settings:
    """Return isolated Settings configured for in-memory tests."""
    return Settings(
        app_env="test",
        api_bearer_token="test-bearer-token",
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        gemini_api_key="test-gemini-key",
        nvidia_nim_api_key="test-nim-key",
        embedding_model="gemini-embedding-2",
        embedding_dim=4,
    )


@pytest.fixture
async def async_engine() -> AsyncIterator[AsyncEngine]:
    """Create an in-memory async SQLite engine with all ORM tables."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def async_session_factory(
    async_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Return an async_sessionmaker bound to the test SQLite engine."""
    return async_sessionmaker(async_engine, expire_on_commit=False, class_=AsyncSession)


@pytest.fixture
async def db_session(
    async_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield a clean AsyncSession for a single test case."""
    async with async_session_factory() as session:
        yield session


@pytest.fixture
def fake_redis() -> FakeRedis:
    """Return an isolated in-memory async Redis stub."""
    return FakeRedis()


@pytest.fixture
def repo(db_session: AsyncSession) -> LifeOSRepository:
    """Return a LifeOSRepository bound to the test AsyncSession."""
    return LifeOSRepository(db_session)
