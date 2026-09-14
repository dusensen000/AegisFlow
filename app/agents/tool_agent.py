"""Execute exactly the tool step dispatched by the supervisor."""

from app.services.agent_bus import make_message
from app.models import Order, LogisticsInfo


class ToolAgent:
    def __init__(self, context):
        self.context = context

    async def run(self, state: dict) -> dict:
        cursor = state["plan_cursor"]
        step = state["plan"][cursor]
        ticket = state["ticket"]
        run_id = state.get("run_id", state["thread_id"])
        await self.context.bus.consume(run_id, "tool_agent")
        result = await self.context.tools.aexecute(
            step["tool_name"], step["arguments"],
            operator_level=state.get("operator_level", 1), actor_id=state.get("actor_id", "internal"),
            allowed_order_id=ticket["order_id"], allowed_user_id=ticket["user_id"],
        )
        result.step_id = step["step_id"]
        evidence_updates = {}
        if result.status == "ok" and result.tool_name in {"query_order", "query_logistics"}:
            try:
                model = Order if result.tool_name == "query_order" else LogisticsInfo
                evidence = model.model_validate(result.data)
                if evidence.order_id != ticket["order_id"] or (model is Order and evidence.user_id != ticket["user_id"]):
                    raise ValueError("Evidence scope mismatch")
                result.data = evidence.model_dump(mode="json")
                evidence_updates["order" if model is Order else "logistics"] = result.data
            except (ValueError, TypeError):
                result.status, result.data, result.error = "error", {}, "查询证据无效或不属于当前工单"
        results = list(state.get("tool_results", []))
        # Latest attempt supersedes earlier data, including a newer failed query.
        results = [r for r in results if not (r["tool_name"] == result.tool_name and r.get("arguments") == result.arguments)]
        results.append(result.model_dump(mode="json"))
        message = make_message(run_id, "tool_agent", "supervisor", "progress",
                               {"step_id": step["step_id"], "result": result.model_dump(mode="json")})
        await self.context.bus.publish(message)
        self.context.tracer.event("tool_call", run_id=run_id, **result.model_dump(mode="json"))
        return {**evidence_updates, "tool_results": results, "plan_cursor": cursor + 1,
                "completed_steps": state.get("completed_steps", []) + [step["step_id"]],
                "tool_call_count": state.get("tool_call_count", 0) + 1,
                "errors": state.get("errors", []) + ([result.error] if result.error else []),
                "messages": [message.model_dump()]}
