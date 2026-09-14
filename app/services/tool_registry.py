"""Typed business tools shared by the local executor and MCP server."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from dataclasses import dataclass
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import AfterSaleRule, LogisticsInfo, Order, ToolCallResult, Ticket
from app.services.policy import assess, money
from app.services.memory_store import InMemoryRateLimiter
from app.services.ticket_context import CURRENT_TICKET


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OrderArguments(ToolArguments):
    order_id: str = Field(min_length=1, max_length=100)


class RulesArguments(ToolArguments):
    category: Literal["退款", "退货", "换货", "补偿", "仅退款", "审核"] | None = None


class EligibilityArguments(OrderArguments):
    requested_action: str = Field(min_length=1, max_length=50)
    ticket_id: str = Field(min_length=1)


class CompensationArguments(OrderArguments):
    damage_level: Literal["low", "medium", "high"]
    ticket_id: str = Field(min_length=1)


class HistoryArguments(ToolArguments):
    user_id: str = Field(min_length=1, max_length=100)


@dataclass
class ToolSpec:
    name: str
    description: str
    argument_model: type[BaseModel]
    permission_level: int
    handler: Callable[..., dict]


class ToolRegistry:
    def __init__(self, orders: list[Order], logistics: list[LogisticsInfo],
                 rules: list[AfterSaleRule], tickets: list[Ticket] | None = None,
                 rate_limit: int = 120, timeout: float = 15):
        self._orders = {order.order_id: order for order in orders}
        self._logistics = {item.order_id: item for item in logistics}
        self._rules = rules
        self._tickets = {ticket.ticket_id: ticket for ticket in tickets or []}
        self._tools: dict[str, ToolSpec] = {}
        self.limiter = InMemoryRateLimiter(rate_limit)
        self.timeout = timeout
        for spec in [
            ToolSpec("query_order", "查询订单状态和实付金额", OrderArguments, 1, self._query_order),
            ToolSpec("query_logistics", "查询物流轨迹与签收时间", OrderArguments, 1, self._query_logistics),
            ToolSpec("query_after_sale_rules", "查询售后规则原文", RulesArguments, 1, self._query_after_sale_rules),
            ToolSpec("check_after_sale_eligibility", "按工单时间和已核验证据校验规则资格", EligibilityArguments, 1, self._check_after_sale_eligibility),
            ToolSpec("calculate_compensation", "按适用规则与核验破损等级计算补偿", CompensationArguments, 1, self._calculate_compensation),
            ToolSpec("query_user_history", "查询用户历史订单与售后记录", HistoryArguments, 2, self._query_user_history),
        ]:
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"重复的工具名: {spec.name}")
        self._tools[spec.name] = spec

    def ticket(self, ticket_id):
        current = CURRENT_TICKET.get()
        return current if current and current.ticket_id == ticket_id else self._tickets.get(ticket_id)

    def list_tools(self) -> list[dict]:
        return [{"name": s.name, "description": s.description,
                 "inputSchema": s.argument_model.model_json_schema(),
                 "permissionLevel": s.permission_level} for s in self._tools.values()]

    def to_function_calling_schemas(self) -> list[dict]:
        return [{"type": "function", "function": {"name": s.name,
                 "description": s.description, "parameters": s.argument_model.model_json_schema()}}
                for s in self._tools.values()]

    def validate(self, name: str, args: dict, operator_level: int = 1,
                 allowed_order_id: str | None = None, allowed_user_id: str | None = None) -> dict:
        spec = self._tools.get(name)
        if spec is None:
            raise ValueError(f"未知工具: {name}")
        if operator_level < spec.permission_level:
            raise ValueError(f"权限不足，需要等级 {spec.permission_level}")
        validated = spec.argument_model.model_validate(args).model_dump()
        if allowed_order_id and validated.get("order_id", allowed_order_id) != allowed_order_id:
            raise ValueError("工具参数中的订单不属于当前工单")
        if allowed_user_id and validated.get("user_id", allowed_user_id) != allowed_user_id:
            raise ValueError("工具参数中的用户不属于当前工单")
        if "ticket_id" in validated:
            ticket = self.ticket(validated["ticket_id"])
            if not ticket or ticket.order_id != validated["order_id"]:
                raise ValueError("工单与订单不匹配")
        return validated

    def execute(self, name: str, args: dict, operator_level: int = 1, **scope) -> ToolCallResult:
        result = ToolCallResult(call_id=uuid.uuid4().hex, tool_name=name, status="error", arguments=args)
        try:
            validated = self.validate(name, args, operator_level, **scope)
            data = self._tools[name].handler(**validated)
            if name == "check_after_sale_eligibility":
                data = self.policy(validated["order_id"], validated["ticket_id"], validated["requested_action"], operator_level)
            result.status, result.data = "ok", data
        except Exception as exc:
            result.error = str(exc)
        return result

    async def aexecute(self, name: str, args: dict, operator_level: int = 1,
                       actor_id: str = "internal", **scope) -> ToolCallResult:
        if not await self.limiter.allow(f"{actor_id}:{name}"):
            return ToolCallResult(tool_name=name, status="error", arguments=args, error="工具调用限流")
        try:
            return await asyncio.wait_for(asyncio.to_thread(self.execute, name, args, operator_level, **scope), self.timeout)
        except asyncio.TimeoutError:
            return ToolCallResult(tool_name=name, status="error", arguments=args, error="工具调用超时")

    def policy(self, order_id: str, ticket_id: str, action: str, operator_level: int = 1) -> dict:
        ticket = self.ticket(ticket_id)
        if not ticket or ticket.order_id != order_id:
            raise ValueError("工单与订单不匹配")
        order = self._orders[order_id]
        history_count = sum(o.after_sale_count for o in self._orders.values() if o.user_id == order.user_id)
        result = assess(order, self._logistics.get(order_id), ticket, action, operator_level, history_count)
        result.update(order_id=order_id, ticket_id=ticket_id)
        return result

    def _query_order(self, order_id: str) -> dict:
        return self._orders[order_id].model_dump(mode="json")

    def _query_logistics(self, order_id: str) -> dict:
        return self._logistics[order_id].model_dump(mode="json")

    def _query_after_sale_rules(self, category: str | None = None) -> dict:
        rules = [r.model_dump() for r in self._rules if not category or r.category == category or category in r.actions]
        return {"rules": rules, "count": len(rules)}

    def _check_after_sale_eligibility(self, order_id: str, requested_action: str, ticket_id: str) -> dict:
        return self.policy(order_id, ticket_id, requested_action)

    def _calculate_compensation(self, order_id: str, damage_level: str, ticket_id: str) -> dict:
        policy = self.policy(order_id, ticket_id, "补偿")
        if not policy["eligible"]:
            raise ValueError("; ".join(policy["blockers"]))
        ticket = self.ticket(ticket_id)
        if "R006" in policy["matched_rules"]:
            if f"damage_{damage_level}" not in ticket.verified_facts:
                raise ValueError("破损等级未经核验")
            rate = {"low": 0.10, "medium": 0.15, "high": 0.20}[damage_level]
        else:
            rate = 0.05
        return {"order_id": order_id, "ticket_id": ticket_id,
                "damage_level": damage_level if "R006" in policy["matched_rules"] else None,
                "basis": "confirmed_damage" if "R006" in policy["matched_rules"] else "logistics_delay",
                "rate": rate, "amount": float(money(money(self._orders[order_id].amount) * Decimal(str(rate)))),
                "matched_rules": policy["matched_rules"]}

    def _query_user_history(self, user_id: str) -> dict:
        orders = [o.model_dump(mode="json") for o in self._orders.values() if o.user_id == user_id]
        return {"user_id": user_id, "order_count": len(orders),
                "after_sale_count": sum(o["after_sale_count"] for o in orders), "orders": orders}
