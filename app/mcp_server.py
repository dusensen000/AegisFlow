"""Independent, authenticated MCP service exposing the typed domain tools."""

import json
import secrets
from mcp.server.fastmcp import FastMCP
from mcp.types import Tool, ToolAnnotations, CallToolResult, TextContent

from app.config import get_settings
from app.data.seed import get_orders, get_logistics, get_rules, get_tickets
from app.services.tool_registry import ToolRegistry
from app.services.ticket_context import CURRENT_TICKET
from app.models import Ticket


class BusinessMCP(FastMCP):
    def __init__(self, registry):
        self.registry = registry
        super().__init__("AegisFlow Business Tools", stateless_http=True, json_response=True)

    async def list_tools(self):
        return [Tool(name=s["name"], description=s["description"], inputSchema=s["inputSchema"],
            annotations=ToolAnnotations(readOnlyHint=True), _meta={"permissionLevel": s["permissionLevel"]})
            for s in self.registry.list_tools()]

    async def call_tool(self, name, arguments):
        meta = self.get_context().request_context.meta
        fields = meta.model_dump() if meta else {}
        snapshot = fields.get("ticket_context")
        token = CURRENT_TICKET.set(Ticket.model_validate(snapshot) if snapshot else None)
        try:
            result = await self.registry.aexecute(name, arguments, operator_level=3, actor_id="mcp-service")
        finally:
            CURRENT_TICKET.reset(token)
        payload = result.model_dump(mode="json")
        return CallToolResult(isError=result.status != "ok", structuredContent=payload,
            content=[TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))])


def create_mcp_app(settings=None, registry=None):
    settings = settings or get_settings()
    registry = registry or ToolRegistry(get_orders(), get_logistics(), get_rules(), get_tickets(),
        settings.tool_rate_limit_per_minute, settings.tool_timeout_seconds)
    service = BusinessMCP(registry)
    app = service.streamable_http_app()
    if not settings.mcp_api_key:
        raise RuntimeError("MCP HTTP service requires MCP_API_KEY")

    async def authenticate(request, call_next):
        from starlette.responses import JSONResponse
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied, "Bearer " + settings.mcp_api_key):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)
    from starlette.middleware.base import BaseHTTPMiddleware
    app.add_middleware(BaseHTTPMiddleware, dispatch=authenticate)
    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_mcp_app(), host="127.0.0.1", port=8001)
