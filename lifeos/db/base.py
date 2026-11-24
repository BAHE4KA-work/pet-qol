"""SQLAlchemy async base, Dialect-aware Vector type, and session factory."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Text, TypeDecorator
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all LifeOS ORM models."""


class EmbeddingVector(TypeDecorator[list[float]]):
    """Stores pgvector Vector(dim) on PostgreSQL and JSON float list on SQLite."""

    impl = Text
    cache_ok = True

    def __init__(self, dim: int = 768) -> None:
        super().__init__()
        self.dim = dim

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Vector(self.dim))
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value: list[float] | None, dialect: Any) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value
        return json.dumps([float(x) for x in value])

    def process_result_value(self, value: Any, dialect: Any) -> list[float] | None:
        if value is None:
            return None
        if isinstance(value, str):
            return [float(x) for x in json.loads(value)]
        return [float(x) for x in value]


def create_engine_and_sessionmaker(
    database_url: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Create async SQLAlchemy engine and session factory."""
    engine = create_async_engine(database_url, echo=False, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return engine, session_factory


async def get_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession and ensure proper cleanup."""
    async with session_factory() as session:
        yield session
