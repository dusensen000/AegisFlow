import socket
import threading
import time
import httpx
import pytest
import uvicorn
from app.mcp_server import create_mcp_app
from app.services.mcp_client import MCPToolClient
from app.services.tool_registry import ToolRegistry, ToolSpec, OrderArguments
from app.data.seed import get_orders, get_logistics, get_rules, get_tickets


@pytest.fixture
def mcp_url(context):
    context.settings.mcp_api_key = "offline-service-token"
    registry = ToolRegistry(get_orders(), get_logistics(), get_rules(), get_tickets())
    registry.register(ToolSpec("query_invoice", "查询发票", OrderArguments, 1,
        lambda order_id: {"order_id": order_id, "invoice_status": "issued"}))
    app = create_mcp_app(context.settings, registry)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=5, timeout_keep_alive=1))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        if not thread.is_alive():
            raise AssertionError("MCP service stopped during startup")
        time.sleep(0.02)
    assert server.started
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(10)
    sock.close()
    assert not thread.is_alive()


@pytest.mark.asyncio
async def test_real_mcp_initialize_list_call_and_auth(context, mcp_url):
    async with httpx.AsyncClient() as client:
        assert (await client.post(mcp_url, json={})).status_code == 401
    gateway = MCPToolClient(context.tools, mcp_url, "offline-service-token")
    await gateway.open()
    response = await gateway.aexecute("query_order", {"order_id": "SO20260114002"}, allowed_order_id="SO20260114002")
    assert response.status == "ok", response.error
    assert response.data["amount"] == 399
    assert "query_invoice" in {tool["name"] for tool in gateway.list_tools()}
    invoice = await gateway.aexecute("query_invoice", {"order_id": "SO20260114002"}, allowed_order_id="SO20260114002")
    assert invoice.status == "ok" and invoice.data["invoice_status"] == "issued"
    assert (await gateway.aexecute("query_invoice", {"order_id": "SO20260101001"}, allowed_order_id="SO20260114002")).status == "error"
    assert (await gateway.aexecute("query_user_history", {"user_id": "U10002"}, operator_level=1)).status == "error"
    async with gateway.session() as session:
        invalid = await session.call_tool("calculate_compensation", {"order_id": "SO20260114002", "ticket_id": "T20260203002", "damage_level": "INVALID"})
        assert invalid.isError
        invalid_type = await session.call_tool("query_order", {"order_id": 123})
        assert invalid_type.isError


@pytest.mark.asyncio
async def test_mcp_accepts_platform_owned_custom_ticket_snapshot(context, mcp_url):
    from app.models import Ticket
    from app.services.ticket_context import CURRENT_TICKET
    ticket = Ticket(ticket_id="T-CUSTOM-MCP", order_id="SO20260201005", user_id="U10005", intent="退款",
        description="取消未发货订单", user_claim="取消订单", requested_action="仅退款")
    gateway = MCPToolClient(context.tools,mcp_url,"offline-service-token")
    await gateway.open()
    token = CURRENT_TICKET.set(ticket)
    try:
        result = await gateway.aexecute("check_after_sale_eligibility", {
            "ticket_id":ticket.ticket_id,"order_id":ticket.order_id,"requested_action":"仅退款"},allowed_order_id=ticket.order_id)
        assert result.status == "ok",result.error
        assert result.data["eligible"]
        assert result.data["ticket_id"] == ticket.ticket_id
    finally:
        CURRENT_TICKET.reset(token)
