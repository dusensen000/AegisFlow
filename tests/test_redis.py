import asyncio
import fakeredis
import pytest
from app.services.agent_bus import RedisAgentMessageBus, make_message
from app.services.memory_store import RedisRateLimiter
from app.services.repository import DurableSessionStore
from app.services.cache import InMemoryCache
from app.services.rag import SimpleRAGRetriever


@pytest.mark.asyncio
async def test_redis_bus_atomically_consumes_once_across_clients():
    server = fakeredis.FakeServer()
    first, second = RedisAgentMessageBus("redis://unused"), RedisAgentMessageBus("redis://unused")
    first._client = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    second._client = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    await first.publish(make_message("run", "reviewer", "planner", "review", {}))
    deliveries = await asyncio.gather(first.consume("run", "planner"), second.consume("run", "planner"))
    assert sum(len(messages) for messages in deliveries) == 1
    assert await first._client.ttl(first._key("run")) > 0
    await first._client.aclose()
    await second._client.aclose()


@pytest.mark.asyncio
async def test_redis_limiter_sets_expiry_atomically():
    limiter = RedisRateLimiter("redis://unused", limit_per_minute=2)
    limiter._client = fakeredis.FakeAsyncRedis(decode_responses=True)
    results = await asyncio.gather(*(limiter.allow("actor") for _ in range(3)))
    assert sum(results) == 2
    assert await limiter._client.ttl("aegisflow:ratelimit:actor") > 0
    await limiter._client.aclose()


@pytest.mark.asyncio
async def test_knowledge_version_invalidates_cached_results():
    cache = InMemoryCache()
    first = SimpleRAGRetriever([{"id":"R1","title":"refund","content":"old refund rule","category":"refund"}],cache=cache)
    second = SimpleRAGRetriever([{"id":"R1","title":"refund","content":"new refund rule","category":"refund"}],cache=cache)
    assert (await first.aretrieve("refund"))[0]["content"] == "old refund rule"
    assert (await second.aretrieve("refund"))[0]["content"] == "new refund rule"
