"""MCP transport adapter; platform RBAC and ticket scope are checked before I/O."""

import asyncio
import uuid
import httpx
from contextlib import asynccontextmanager
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from jsonschema import Draft202012Validator

from app.models import ToolCallResult
from app.services.ticket_context import CURRENT_TICKET


class MCPToolClient:
    def __init__(self, registry, url, api_key):
        self.registry, self.url, self.api_key = registry, url, api_key
        self.definitions = registry.list_tools()

    @asynccontextmanager
    async def session(self):
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + self.api_key},
                                     timeout=self.registry.timeout, trust_env=False) as client:
            async with streamable_http_client(self.url, http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    async def open(self):
        async with self.session() as session:
            listing = await session.list_tools()
            remote = {tool.name: tool for tool in listing.tools}
            for definition in self.definitions:
                tool = remote.get(definition["name"])
                if not tool or tool.inputSchema != definition["inputSchema"]:
                    raise RuntimeError(f"MCP tool contract mismatch: {definition['name']}")
            definitions = []
            for tool in listing.tools:
                Draft202012Validator.check_schema(tool.inputSchema)
                level = (tool.meta or {}).get("permissionLevel", 3)
                if not isinstance(level, int) or not 1 <= level <= 3:
                    raise RuntimeError("Invalid MCP permission metadata")
                definitions.append({"name": tool.name, "description": tool.description,
                    "inputSchema": tool.inputSchema, "permissionLevel": level})
            self.definitions = definitions

    def validate(self, name, args, operator_level=1, allowed_order_id=None, allowed_user_id=None):
        if name in self.registry._tools:
            return self.registry.validate(name, args, operator_level, allowed_order_id, allowed_user_id)
        definition = next((tool for tool in self.definitions if tool["name"] == name), None)
        if not definition or operator_level < definition["permissionLevel"]:
            raise ValueError("Unknown or unauthorized MCP tool")
        Draft202012Validator(definition["inputSchema"]).validate(args)
        if allowed_order_id and args.get("order_id", allowed_order_id) != allowed_order_id:
            raise ValueError("Tool order is outside the current ticket")
        if allowed_user_id and args.get("user_id", allowed_user_id) != allowed_user_id:
            raise ValueError("Tool user is outside the current ticket")
        return dict(args)

    def list_tools(self):
        return self.definitions

    def to_function_calling_schemas(self):
        return [{"type": "function", "function": {"name": tool["name"],
                 "description": tool["description"], "parameters": tool["inputSchema"]}}
                for tool in self.definitions]

    async def aexecute(self, name, args, operator_level=1, actor_id="internal", **scope):
        result = ToolCallResult(call_id=uuid.uuid4().hex, tool_name=name, arguments=args, status="error")
        try:
            validated = self.validate(name, args, operator_level, **scope)
            if not await self.registry.limiter.allow(f"{actor_id}:{name}"):
                raise ValueError("工具调用限流")
            async def invoke():
                async with self.session() as session:
                    current = CURRENT_TICKET.get()
                    meta = {"ticket_context": current.model_dump(mode="json")} if current else None
                    return await session.call_tool(name, validated, meta=meta)
            response = await asyncio.wait_for(invoke(), timeout=self.registry.timeout)
            payload = ToolCallResult.model_validate(response.structuredContent)
            if payload.tool_name != name or payload.arguments != validated:
                raise ValueError("MCP response does not match the request")
            result.status, result.data, result.error = payload.status, payload.data, payload.error
            if response.isError:
                result.status = "error"
        except Exception as exc:
            result.error = f"MCP: {type(exc).__name__}"
        return result
