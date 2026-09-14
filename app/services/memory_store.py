"""会话状态存储与限流器。"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from collections import deque


class SessionStore(ABC):
    @abstractmethod
    async def get(self, thread_id: str) -> dict | None:
        raise NotImplementedError

    @abstractmethod
    async def set(self, thread_id: str, state: dict) -> None:
        raise NotImplementedError

    @abstractmethod
    async def delete(self, thread_id: str) -> None:
        raise NotImplementedError


class InMemorySessionStore(SessionStore):
    def __init__(self) -> None:
        self._data: dict[str, dict] = {}

    async def get(self, thread_id: str) -> dict | None:
        return self._data.get(thread_id)

    async def set(self, thread_id: str, state: dict) -> None:
        self._data[thread_id] = state

    async def delete(self, thread_id: str) -> None:
        self._data.pop(thread_id, None)


class RedisSessionStore(SessionStore):
    def __init__(self, redis_url: str, ttl_seconds: int = 3600) -> None:
        self._redis_url = redis_url
        self._ttl_seconds = ttl_seconds
        self._client = None

    async def _conn(self):
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(self._redis_url, decode_responses=True)
        return self._client

    async def get(self, thread_id: str) -> dict | None:
        conn = await self._conn()
        raw = await conn.get(self._key(thread_id))
        return json.loads(raw) if raw else None

    async def set(self, thread_id: str, state: dict) -> None:
        conn = await self._conn()
        await conn.set(
            self._key(thread_id),
            json.dumps(state, ensure_ascii=False),
            ex=self._ttl_seconds,
        )

    async def delete(self, thread_id: str) -> None:
        conn = await self._conn()
        await conn.delete(self._key(thread_id))

    @staticmethod
    def _key(thread_id: str) -> str:
        return f"aegisflow:session:{thread_id}"


class RateLimiter(ABC):
    @abstractmethod
    async def allow(self, key: str, cost: int = 1) -> bool:
        raise NotImplementedError


class InMemoryRateLimiter(RateLimiter):
    def __init__(self, limit_per_minute: int = 60) -> None:
        self._limit = limit_per_minute
        self._records: dict[str, deque[float]] = {}

    async def allow(self, key: str, cost: int = 1) -> bool:
        now = time.time()
        records = self._records.setdefault(key, deque())
        while records and now - records[0] > 60:
            records.popleft()
        if len(records) + cost > self._limit:
            return False
        for _ in range(cost):
            records.append(now)
        return True


class RedisRateLimiter(RateLimiter):
    def __init__(self, redis_url: str, limit_per_minute: int = 60) -> None:
        self._redis_url = redis_url
        self._limit = limit_per_minute
        self._client = None

    async def _conn(self):
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(self._redis_url, decode_responses=True)
        return self._client

    async def allow(self, key: str, cost: int = 1) -> bool:
        conn = await self._conn()
        redis_key = f"aegisflow:ratelimit:{key}"
        count = await conn.eval("""
            local count = redis.call('INCRBY', KEYS[1], ARGV[1])
            if count == tonumber(ARGV[1]) then redis.call('EXPIRE', KEYS[1], 60) end
            return count
        """, 1, redis_key, cost)
        return count <= self._limit
