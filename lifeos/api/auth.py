"""Bearer token authentication and shared FastAPI request dependencies."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from lifeos.config import Settings, get_settings

_bearer_scheme = HTTPBearer(auto_error=False)


def require_bearer_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> str:
    """Validate incoming HTTP Bearer token with constant-time comparison."""
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not settings.api_bearer_token
        or not secrets.compare_digest(credentials.credentials, settings.api_bearer_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession from `app.state.session_factory`."""
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database session factory is not initialized",
        )
    async with session_factory() as session:
        yield session


def get_today_for_settings(settings: Settings) -> date:
    """Return today's date in the configured application timezone."""
    try:
        tz = ZoneInfo(settings.timezone)
    except ZoneInfoNotFoundError:
        return datetime.now(UTC).date()
    return datetime.now(tz).date()
