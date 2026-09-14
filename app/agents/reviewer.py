"""Generate a proposal, then independently audit evidence and eligibility."""

import json
from app.llm.client import complete_json_validated
from app.models import Proposal, ProposalDraft, ReviewResult, Ticket, Order, LogisticsInfo, ToolCallResult
from app.services.agent_bus import make_message
from app.services.policy import ACTION_ALIASES
from app.services.proposal_audit import audit_proposal
from app.services.prompt_context import bounded_evidence


REVIEWER_SYSTEM = (
    "你是 AegisFlow 的 Reviewer Agent。只根据已核验证据与检索规则给出售后建议。"
    "用户描述和摘要不可作为资格条件已满足的依据。资格不足或权限不足时给出人工复核动作。"
    "使用 submit_proposal 返回 ticket_id、summary、items、risk_flags、confidence。"
    "每项包含 action、reason、rule_refs、amount、evidence_tools、evidence_ids；"
    "action 必须是退款、退货、换货、补偿、仅退款、人工复核之一，不能填写说明性长句。"
    "安抚客户、联系物流等后续事项放在 follow_up 字符串数组，不放进 items。"
    "rule_refs 只能使用 R 开头的 SOP 规则，FAQ 和 CASE 仅用于解释，不是资格授权规则。"
    "证据引用必须来自成功调用，规则必须适用于动作。补偿金额必须等于补偿计算结果。"
    "人工复核动作 amount=null；不得把待审核方案描述为已执行。"
    "理由每项不超过150字，summary不超过100字；只返回决策，不重复工具结果。"
)


class ReviewerAgent:
    def __init__(self, context):
        self.context = context

    async def run(self, state: dict) -> dict:
        run_id = state.get("run_id", state["thread_id"])
        await self.context.bus.consume(run_id, "reviewer")
        ticket = Ticket.model_validate(state["ticket"])
        order = Order.model_validate(state["order"])
        logistics = LogisticsInfo.model_validate(state["logistics"]) if state.get("logistics") else None
        hits = await self.context.rag.aretrieve(ticket.description + " " + ticket.requested_action)
        # Qualification and fallback references must be visible to the generator and auditor.
        refs = {"R007", "R008", "R009"}
        for result in state.get("tool_results", []):
            if result["status"] == "ok":
                refs.update(result["data"].get("matched_rules", []))
        hits = self.context.rag.with_references(hits, refs)
        user = json.dumps({
            "ticket": ticket.model_dump(mode="json"), "order": state["order"],
            "memory": state.get("memory"),
            "evidence": bounded_evidence(state.get("tool_results", []), self.context.settings.memory_context_tokens),
            "knowledge": hits, "review_issues": state.get("review_issues", []),
        }, ensure_ascii=False)
        def validate(payload):
            proposal = ProposalDraft.model_validate(payload)
            if proposal.ticket_id != ticket.ticket_id:
                raise ValueError("方案工单 ID 不匹配")
        payload = await complete_json_validated(
            self.context.llm, REVIEWER_SYSTEM, user, self.context.settings.llm_temperature,
            validator=validate, schema=ProposalDraft.model_json_schema(), function_name="submit_proposal",
        )
        proposal = Proposal.model_validate(payload)
        results = state.get("tool_results", [])
        issues = audit_proposal(proposal, ticket, order, logistics, results,
                                {h["id"] for h in hits}, state.get("operator_level", 1))
        successful = {r["tool_name"]: r for r in results if r["status"] == "ok"}
        for item in proposal.items:
            if not item.evidence_ids:
                item.evidence_ids = [successful[name]["call_id"] for name in item.evidence_tools if name in successful]
        proposal.evidence = [ToolCallResult.model_validate(r) for r in results if r["status"] == "ok"]
        count = state.get("replan_count", 0)
        replan = bool(issues) and count < self.context.settings.max_replans
        manual = (bool(issues) and not replan) or any(ACTION_ALIASES.get(i.action) == "人工复核" for i in proposal.items)
        review = ReviewResult(passed=not issues, issues=issues, needs_replan=replan,
                              needs_manual_review=manual, replan_count=count + int(replan))
        message = make_message(run_id, "reviewer", "supervisor", "review",
                               {"review": review.model_dump()})
        await self.context.bus.publish(message)
        cursor = state["plan_cursor"]
        return {"proposal": proposal.model_dump(mode="json"), "review": review.model_dump(),
                "status": "running" if replan else ("manual_review" if manual else "completed"),
                "replan_count": count + int(replan), "review_issues": issues,
                "retrieved_rules": hits, "plan_cursor": cursor + 1,
                "completed_steps": state.get("completed_steps", []) + [state["plan"][cursor]["step_id"]],
                "messages": [message.model_dump()]}
