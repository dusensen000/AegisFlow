"""Supervisor dispatches one worker at a time and owns the execution budget."""

from app.services.agent_bus import make_message

TERMINAL = {"completed", "failed", "manual_review", "cancelled"}


class SupervisorAgent:
    def __init__(self, context):
        self.context = context

    async def run(self, state: dict) -> dict:
        run_id = state.get("run_id", state["thread_id"])
        await self.context.bus.consume(run_id, "supervisor")
        if state.get("status") in TERMINAL:
            return {"next_agent": "end"}
        if state.get("step_count", 0) >= state["max_steps"]:
            return {"status": "manual_review", "next_agent": "end",
                    "errors": state.get("errors", []) + ["达到 MAX_STEPS，已停止执行并转人工复核"],
                    "review": {"passed": False, "issues": ["执行预算耗尽"],
                               "needs_manual_review": True, "needs_replan": False}}
        last = state.get("last_worker")
        if not last or (last == "reviewer" and (state.get("review") or {}).get("needs_replan")):
            target = "planner"
        else:
            plan = state.get("plan", [])
            cursor = state.get("plan_cursor", 0)
            if cursor >= len(plan):
                return {"status": "failed", "next_agent": "end",
                        "errors": state.get("errors", []) + ["计划未产生终态"]}
            step = plan[cursor]
            if not set(step.get("depends_on", [])).issubset(state.get("completed_steps", [])):
                return {"status": "failed", "next_agent": "end",
                        "errors": state.get("errors", []) + ["计划依赖尚未完成"]}
            target = {"tool": "tool_agent", "memory": "memory_agent", "reviewer": "reviewer"}[step["agent"]]
        message = make_message(run_id, "supervisor", target, "task",
                               {"plan_cursor": state.get("plan_cursor", 0), "step_count": state.get("step_count", 0)})
        await self.context.bus.publish(message)
        return {"next_agent": target, "messages": [message.model_dump()]}
