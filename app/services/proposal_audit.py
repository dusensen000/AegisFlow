"""Evidence, rule applicability and money checks independent of model output."""

from decimal import Decimal, InvalidOperation
from app.models import Proposal, Ticket, Order, LogisticsInfo
from app.services.policy import ACTION_ALIASES, assess, money


def audit_proposal(proposal: Proposal, ticket: Ticket, order: Order,
                   logistics: LogisticsInfo | None, results: list[dict],
                   retrieved_ids: set[str], operator_level: int = 1) -> list[str]:
    issues = []
    successful = {r["tool_name"]: r for r in results if r["status"] == "ok"}
    calls = {r.get("call_id"): r for r in results if r["status"] == "ok" and r.get("call_id")}
    for name in ("query_order", "query_logistics", "check_after_sale_eligibility"):
        result = successful.get(name)
        if not result or result["data"].get("order_id") != ticket.order_id:
            issues.append(f"缺少当前订单的有效证据: {name}")
    eligibility = [r for r in results if r["tool_name"] == "check_after_sale_eligibility"
        and r["status"] == "ok" and r["data"].get("ticket_id") == ticket.ticket_id
        and r.get("arguments", {}).get("requested_action") == ticket.requested_action]
    if not eligibility:
        issues.append("缺少当前工单申请动作的资格校验记录")
    history_count = max([order.after_sale_count] + [r["data"].get("user_after_sale_count", 0) for r in eligibility])
    if not proposal.items:
        issues.append("未生成任何方案项")
    if proposal.ticket_id != ticket.ticket_id:
        issues.append("方案工单 ID 不匹配")
    total = Decimal("0")
    seen_actions = set()
    for item in proposal.items:
        action = ACTION_ALIASES.get(item.action, item.action)
        policy = assess(order, logistics, ticket, action, operator_level, history_count)
        if not policy["eligible"]:
            issues.extend(f"{action}: {b}" for b in policy["blockers"])
        if not item.rule_refs or any(ref not in retrieved_ids or ref not in policy["matched_rules"] for ref in item.rule_refs):
            issues.append(f"{action}: 规则引用不存在、未检索到或不适用于该动作")
        if not item.evidence_tools or any(name not in successful for name in item.evidence_tools):
            issues.append(f"{action}: 证据工具未成功执行")
        if item.evidence_ids and any(cid not in calls for cid in item.evidence_ids):
            issues.append(f"{action}: 证据调用 ID 无效")
        if item.evidence_ids and not set(item.evidence_tools).issubset({calls[cid]["tool_name"] for cid in item.evidence_ids if cid in calls}):
            issues.append(f"{action}: 证据调用 ID 与工具引用不一致")
        if action in seen_actions:
            issues.append(f"重复的售后动作: {action}")
        seen_actions.add(action)
        if action in {"退款", "退货", "补偿", "仅退款"}:
            try:
                value = money(item.amount) if item.amount is not None else Decimal("NaN")
                if not value.is_finite() or value < Decimal(str(policy.get("min_amount", 0))) or value > Decimal(str(policy["max_amount"])):
                    issues.append(f"{action}: 金额缺失或超出规则范围")
                elif value != Decimal(str(item.amount)):
                    issues.append(f"{action}: 金额最多保留两位小数")
                else:
                    total += value
                    if action == "补偿" and "R004" in item.rule_refs and value > money(money(order.amount) * Decimal("0.10")):
                        issues.append("物流补偿金额超出 R004 的 10% 上限")
            except (InvalidOperation, ValueError, TypeError):
                issues.append(f"{action}: 无效金额")
        elif item.amount not in (None, 0):
            issues.append(f"{action}: 非金额动作不能填写赔付金额")
        if action == "补偿":
            calculation = successful.get("calculate_compensation")
            if not calculation or calculation["data"].get("amount") != item.amount or calculation["data"].get("order_id") != ticket.order_id:
                issues.append("补偿金额缺少计算工具的对应依据")
    if total > money(order.amount):
        issues.append("方案累计退款与补偿金额超过实付金额")
    if "换货" in seen_actions and seen_actions.intersection({"退款", "退货", "仅退款"}):
        issues.append("换货与退款方案互斥")
    if len(seen_actions.intersection({"退款", "退货", "仅退款"})) > 1:
        issues.append("多个退款动作互斥")
    return list(dict.fromkeys(issues))
