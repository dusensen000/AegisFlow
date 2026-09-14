"""Deterministic qualification and monetary limits for the demo SOP."""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from app.models import Order, LogisticsInfo, Ticket


ACTION_ALIASES = {
    "退款": "退款", "原路退款": "退款", "退货": "退货", "退货退款": "退货",
    "换货": "换货", "补偿": "补偿", "部分退款": "补偿", "补偿优惠券": "补偿",
    "发放补偿优惠券": "补偿", "仅退款": "仅退款",
    "转人工审核": "人工复核", "人工复核": "人工复核", "拒绝并说明原因": "人工复核",
}


def utc(value: datetime | str) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def money(value: float | Decimal) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def assess(order: Order, logistics: LogisticsInfo | None, ticket: Ticket,
           action: str, operator_level: int = 1, history_count: int | None = None) -> dict:
    action = ACTION_ALIASES.get(action, action)
    facts = set(ticket.verified_facts)
    as_of = utc(ticket.opened_at)
    delivered = next((utc(e.time) for e in reversed(logistics.events)
                      if e.status == "已签收"), None) if logistics else None
    age = (as_of - delivered).total_seconds() / 86400 if delivered else None
    last_update = max((utc(e.time) for e in logistics.events), default=None) if logistics else None
    stale = (as_of - last_update).total_seconds() / 3600 if last_update else None
    applicable: list[str] = []
    blockers: list[str] = []
    required_level = 1
    total_after_sales = max(order.after_sale_count, history_count or 0)
    if action == "人工复核":
        ref = "R007" if order.protection_expired else ("R008" if order.amount >= 500 or total_after_sales >= 3 else "R009")
        return {"eligible": True, "action": action, "matched_rules": [ref],
                "blockers": [], "permission_level": 1, "max_amount": 0, "manual_required": True}
    if action not in {"退款", "退货", "换货", "补偿", "仅退款"}:
        blockers.append("无法识别的售后动作")
    if order.protection_expired:
        blockers.append("订单超出售后保障期，需人工审核")
        required_level = max(required_level, 2)
    if total_after_sales >= 3 or order.amount >= 500:
        blockers.append("高频售后或大额订单，需高级客服复核")
        required_level = 3
    if age is not None and age < 0:
        blockers.append("签收时间晚于工单时间，数据需复核")
    if order.status.value == "已关闭":
        blockers.append("订单已关闭，需人工核验款项和售后记录")
    if order.status.value == "待发货" and delivered is not None:
        blockers.append("订单与物流状态不一致")
    if action == "退款" and age is not None and 0 <= age <= 7 and order.status.value == "已签收":
        applicable = ["R001"]
    elif action == "退货" and order.status.value == "已签收" and age is not None and 0 <= age <= 15 and "product_intact" in facts:
        applicable = ["R002"]
    elif action == "换货" and order.status.value == "已签收" and age is not None and 0 <= age <= 30 and "quality_confirmed" in facts:
        applicable = ["R003"]
    elif action == "仅退款" and (order.status.value == "待发货" or "missing_confirmed" in facts):
        applicable = ["R005"]
    elif action == "补偿":
        if order.status.value == "已发货" and stale is not None and stale >= 48:
            applicable.append("R004")
        if "damage_confirmed" in facts and order.status.value in {"已发货", "已签收"}:
            applicable.append("R006")
    if not applicable:
        blockers.append("缺少满足规则条件的证据，需补充材料或人工审核")
    if operator_level < required_level:
        blockers.append(f"需要客服权限等级 {required_level}")
    maximum = money(order.amount)
    minimum = Decimal("0")
    if action == "退款" or (action == "仅退款" and order.status.value == "待发货"):
        minimum = maximum
    if action == "补偿":
        minimum = money(maximum * Decimal("0.10" if "R006" in applicable else "0.05"))
        maximum = money(maximum * Decimal("0.20" if "R006" in applicable else "0.10"))
    if action == "换货":
        maximum = Decimal("0")
    return {"eligible": not blockers, "action": action, "matched_rules": applicable,
            "blockers": blockers, "permission_level": required_level,
            "min_amount": float(minimum), "max_amount": float(maximum),
            "manual_required": bool(blockers), "days_since_delivery": age,
            "user_after_sale_count": total_after_sales,
            "logistics_stale_hours": stale}
