import asyncio
from datetime import datetime, timezone
import pytest
from app.data.seed import get_ticket, get_order, get_logistics
from app.services.policy import assess
from app.services.agent_bus import InMemoryAgentMessageBus, make_message


def test_strict_types_enums_extra_fields_and_scope(context):
    registry = context.tools
    for args in ({"order_id": 123}, {"order_id": "SO20260114002", "extra": True}):
        assert registry.execute("query_order", args).status == "error"
    args = {"order_id": "SO20260114002", "ticket_id": "T20260203002", "damage_level": "INVALID"}
    assert registry.execute("calculate_compensation", args).status == "error"
    assert registry.execute("query_user_history", {"user_id": "U10002"}, operator_level=1).status == "error"
    assert registry.execute("query_order", {"order_id": "SO20260114002"}, allowed_order_id="another").status == "error"


def test_signed_delivery_window_not_static_boolean():
    ticket = get_ticket("T20260201001").model_copy(update={"opened_at": datetime(2026, 1, 10, tzinfo=timezone.utc)})
    order = get_order(ticket.order_id)
    assert assess(order, get_logistics(order.order_id), ticket, "退款")["eligible"]
    assert not assess(order, get_logistics(order.order_id), get_ticket(ticket.ticket_id), "退款")["eligible"]


def test_logistics_compensation_does_not_invent_damage_evidence(context):
    result = context.tools.execute("calculate_compensation", {
        "order_id": "SO20260114002", "ticket_id": "T20260203002", "damage_level": "low"})
    assert result.status == "ok"
    assert result.data["basis"] == "logistics_delay"
    assert result.data["damage_level"] is None
    from app.services.prompt_context import bounded_evidence
    evidence = bounded_evidence([result.model_dump(mode="json")], 3000)
    assert "arguments" not in evidence[0]


def test_cumulative_history_across_orders_requires_manual_review(context):
    registry = context.tools
    order = registry._orders["SO20260114002"]
    registry._orders[order.order_id] = order.model_copy(update={"after_sale_count": 2})
    registry._orders["history"] = order.model_copy(update={"order_id": "history", "after_sale_count": 2})
    result = registry.policy(order.order_id, "T20260203002", "补偿")
    assert result["user_after_sale_count"] == 4
    assert not result["eligible"]
    assert registry.policy(order.order_id, "T20260203002", "人工复核")["matched_rules"] == ["R008"]


def test_closed_order_cannot_exchange_even_with_quality_evidence():
    from app.models import OrderStatus
    ticket = get_ticket("T20260205003")
    order = get_order(ticket.order_id).model_copy(update={"status": OrderStatus.CLOSED})
    assert not assess(order, get_logistics(ticket.order_id), ticket, "换货")["eligible"]


def test_unbroken_payload_cannot_bypass_context_budget():
    import json
    from app.services.prompt_context import bounded_evidence
    result = {"tool_name": "query_invoice", "call_id": "call", "status": "ok", "data": {"payload": "a" * 10000}}
    evidence = bounded_evidence([result], 256)
    assert evidence[0]["omitted"]
    assert len(json.dumps(evidence).encode()) <= 256


@pytest.mark.asyncio
async def test_bus_consumes_once_and_isolates_runs():
    bus = InMemoryAgentMessageBus()
    await bus.publish(make_message("run-a", "reviewer", "planner", "review", {}))
    assert len(await bus.consume("run-a", "planner", "review")) == 1
    assert await bus.consume("run-a", "planner", "review") == []
    assert await bus.consume("run-b", "planner", "review") == []


@pytest.mark.asyncio
async def test_tool_timeout_and_rate_limit(context):
    import time
    registry = context.tools
    registry.timeout = 0.01
    registry._tools["query_order"].handler = lambda **kwargs: time.sleep(0.03) or {}
    result = await registry.aexecute("query_order", {"order_id": "SO20260114002"})
    assert result.status == "error" and "超时" in result.error
