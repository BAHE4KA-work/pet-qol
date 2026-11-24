"""Daily quota manager for Gemini 4 Hydrogen calls."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any


class QuotaExhaustedError(RuntimeError):
    """Raised when the daily Gemini 4 Hydrogen quota has been exhausted."""


class HydrogenQuotaManager:
    """Enforces daily Gemini 4 Hydrogen usage limits with Redis + in-memory fallback."""

    def __init__(
        self,
        redis_client: Any = None,
        daily_limit: int = 20,
        reserved_system: int = 3,
    ) -> None:
        if daily_limit < 0 or reserved_system < 0:
            raise ValueError("daily_limit and reserved_system must be non-negative")
        self.redis_client = redis_client
        self.daily_limit = daily_limit
        self.reserved_system = reserved_system
        self._memory: dict[str, int] = {}
        self._lock = asyncio.Lock()

    def _quota_key(self, day: date | None = None) -> str:
        target_day = day or date.today()
        return f"lifeos:hydrogen:quota:{target_day.isoformat()}"

    def _limit_for(self, is_system: bool) -> int:
        if is_system:
            return self.daily_limit
        return max(0, self.daily_limit - self.reserved_system)

    async def _get_count(self, key: str) -> int:
        if self.redis_client is None:
            return self._memory.get(key, 0)
        raw = await self.redis_client.get(key)
        if raw is None:
            return 0
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return int(raw)

    async def can_consume(self, is_system: bool = False) -> bool:
        """Return True if at least one quota slot remains for the caller tier."""
        key = self._quota_key()
        used = await self._get_count(key)
        return used < self._limit_for(is_system)

    async def consume(self, is_system: bool = False) -> bool:
        """Attempt to consume one Hydrogen quota slot; return False if exhausted."""
        key = self._quota_key()
        limit = self._limit_for(is_system)

        if self.redis_client is None:
            async with self._lock:
                current = self._memory.get(key, 0)
                if current >= limit:
                    return False
                self._memory[key] = current + 1
                return True

        current = await self._get_count(key)
        if current >= limit:
            return False

        new_val = int(await self.redis_client.incr(key))
        if new_val == 1 and hasattr(self.redis_client, "expire"):
            await self.redis_client.expire(key, 172800)

        if new_val > limit:
            if hasattr(self.redis_client, "decr"):
                await self.redis_client.decr(key)
            return False
        return True

    async def remaining(self, is_system: bool = False) -> dict[str, int]:
        """Return current quota usage and remaining counts."""
        key = self._quota_key()
        used = await self._get_count(key)
        non_system_cap = max(0, self.daily_limit - self.reserved_system)
        limit = self._limit_for(is_system)
        return {
            "used": used,
            "daily_limit": self.daily_limit,
            "reserved_system": self.reserved_system,
            "non_system_remaining": max(0, non_system_cap - used),
            "system_remaining": max(0, self.daily_limit - used),
            "remaining": max(0, limit - used),
        }
