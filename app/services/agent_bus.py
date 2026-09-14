"""Agent 间消息总线：消息传递与共享上下文。"""

from __future__ import annotations

import json
import time
import uuid
from abc import ABC, abstractmethod

from pydantic import BaseModel, Field


class AgentMessage(BaseModel):
    message_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    thread_id: str
    source_agent: str
    target_agent: str
    channel: str
    payload: dict = Field(default_factory=dict)
    timestamp: float = Field(default_factory=time.time)


class AgentMessageBus(ABC):
    @abstractmethod
    async def publish(self, message: AgentMessage) -> None:
        raise NotImplementedError

    @abstractmethod
    async def consume(
        self,
        thread_id: str,
        agent_name: str,
        channel: str | None = None,
    ) -> list[AgentMessage]:
        raise NotImplementedError

    @abstractmethod
    async def history(self, thread_id: str) -> list[AgentMessage]:
        raise NotImplementedError


class InMemoryAgentMessageBus(AgentMessageBus):
    """进程内消息总线，消息按 thread_id 隔离。"""

    def __init__(self) -> None:
        self._messages: dict[str, list[AgentMessage]] = {}
        self._consumed: dict[tuple[str, str], set[str]] = {}

    async def publish(self, message: AgentMessage) -> None:
        self._messages.setdefault(message.thread_id, []).append(message)

    async def consume(
        self,
        thread_id: str,
        agent_name: str,
        channel: str | None = None,
    ) -> list[AgentMessage]:
        messages = self._messages.get(thread_id, [])
        consumed = self._consumed.setdefault((thread_id, agent_name), set())
        output = [
            message
            for message in messages
            if message.target_agent in (agent_name, "all")
            and (channel is None or message.channel == channel)
            and message.message_id not in consumed
        ]
        consumed.update(message.message_id for message in output)
        return output

    async def history(self, thread_id: str) -> list[AgentMessage]:
        return list(self._messages.get(thread_id, []))

    async def delete(self, thread_id: str) -> None:
        self._messages.pop(thread_id, None)
        for key in list(self._consumed):
            if key[0] == thread_id:
                self._consumed.pop(key)


class RedisAgentMessageBus(AgentMessageBus):
    """基于 Redis List 的消息总线，支持跨进程。"""

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._client = None

    async def _conn(self):
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(self._redis_url, decode_responses=True)
        return self._client

    async def publish(self, message: AgentMessage) -> None:
        conn = await self._conn()
        async with conn.pipeline(transaction=True) as pipe:
            pipe.rpush(self._key(message.thread_id), message.model_dump_json())
            pipe.expire(self._key(message.thread_id), 3600)
            await pipe.execute()

    async def consume(
        self,
        thread_id: str,
        agent_name: str,
        channel: str | None = None,
    ) -> list[AgentMessage]:
        conn = await self._conn()
        script = """
            local output = {}
            for _, raw in ipairs(redis.call('LRANGE', KEYS[1], 0, -1)) do
                local msg = cjson.decode(raw)
                if (msg.target_agent == ARGV[1] or msg.target_agent == 'all')
                    and (ARGV[2] == '' or msg.channel == ARGV[2])
                    and redis.call('SADD', KEYS[2], msg.message_id) == 1 then
                    table.insert(output, raw)
                end
            end
            redis.call('EXPIRE', KEYS[2], 3600)
            return output
        """
        rows = await conn.eval(script, 2, self._key(thread_id), self._key(thread_id) + ":consumed:" + agent_name, agent_name, channel or "")
        return [AgentMessage.model_validate_json(row) for row in rows]

    async def history(self, thread_id: str) -> list[AgentMessage]:
        conn = await self._conn()
        raw_messages = await conn.lrange(self._key(thread_id), 0, -1)
        return [AgentMessage.model_validate_json(raw) for raw in raw_messages]

    @staticmethod
    def _key(thread_id: str) -> str:
        return f"aegisflow:bus:{thread_id}"


def make_message(
    thread_id: str,
    source_agent: str,
    target_agent: str,
    channel: str,
    payload: dict,
) -> AgentMessage:
    return AgentMessage(
        thread_id=thread_id,
        source_agent=source_agent,
        target_agent=target_agent,
        channel=channel,
        payload=payload,
    )
