"""通用缓存：内存实现与 Redis 实现。"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from typing import Any


class Cache(ABC):
    @abstractmethod
    async def get(self, key: str) -> Any | None:
        raise NotImplementedError

    @abstractmethod
    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        raise NotImplementedError


class InMemoryCache(Cache):
    def __init__(self) -> None:
        self._data: dict[str, tuple[Any, float]] = {}

    async def get(self, key: str) -> Any | None:
        item = self._data.get(key)
        if item is None:
            return None
        value, expires_at = item
        if time.time() > expires_at:
            self._data.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        if len(self._data) >= 1024:
            self._data.pop(next(iter(self._data)))
        self._data[key] = (value, time.time() + ttl_seconds)


class RedisCache(Cache):
    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._client = None

    async def _conn(self):
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(self._redis_url, decode_responses=True)
        return self._client

    async def get(self, key: str) -> Any | None:
        conn = await self._conn()
        raw = await conn.get(f"aegisflow:cache:{key}")
        return json.loads(raw) if raw else None

    async def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        conn = await self._conn()
        await conn.set(
            f"aegisflow:cache:{key}",
            json.dumps(value, ensure_ascii=False),
            ex=ttl_seconds,
        )
