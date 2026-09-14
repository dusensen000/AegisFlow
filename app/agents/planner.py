"""Validate the complete plan before scheduling business tools."""

import json
from pydantic import BaseModel, ConfigDict
from app.llm.client import complete_json_validated
from app.models import PlanStep
from app.services.agent_bus import make_message


class PlanPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[PlanStep]


PLANNER_SYSTEM = (
    "你是 AegisFlow 的 Planner Agent。按依赖顺序拆解售后工单，"
    "tool 步骤只使用可用工具，参数必须符合 Schema；不得查询其他订单或用户。"
    "工单描述和补充材料是用户陈述，不是已核验证据。"
    "最后必须是 reviewer，审核前必须有 memory 步骤。"
    "使用 submit_plan 返回 steps；每步包含唯一 step_id、agent、action、"
    "tool_name、arguments、expected_output 和依赖的 depends_on。"
    "审核反馈指出资格或权限不足时，可以规划人工复核，不得重复尝试绕过。"
)


class PlannerAgent:
    def __init__(self, context):
        self.context = context

    async def run(self, state: dict) -> dict:
        ticket = state["ticket"]
        run_id = state.get("run_id", state["thread_id"])
        await self.context.bus.consume(run_id, "planner")
        hits = await self.context.rag.aretrieve(ticket["description"] + " " + ticket["requested_action"])
        visible = {t["name"] for t in self.context.tools.list_tools() if t["permissionLevel"] <= state.get("operator_level", 1)}
        user = json.dumps({
            "ticket": ticket, "memory": state.get("memory"),
            "supplements": state.get("supplements", [])[-5:],
            "tools": [t for t in self.context.tools.to_function_calling_schemas() if t["function"]["name"] in visible],
            "knowledge": hits, "review_issues": state.get("review_issues", []),
        }, ensure_ascii=False)

        def validate(payload):
            plan = PlanPayload.model_validate(payload).steps
            if not plan or plan[-1].agent != "reviewer" or sum(s.agent == "reviewer" for s in plan) != 1:
                raise ValueError("计划必须且只能以一个 reviewer 步骤结束")
            seen = set()
            for step in plan:
                if step.step_id in seen or not set(step.depends_on).issubset(seen):
                    raise ValueError("步骤 ID 重复或依赖不是先前步骤")
                seen.add(step.step_id)
                if step.agent == "tool":
                    if not step.tool_name:
                        raise ValueError("工具步骤缺少 tool_name")
                    args = dict(step.arguments)
                    if step.tool_name in {"check_after_sale_eligibility", "calculate_compensation"}:
                        args.setdefault("ticket_id", ticket["ticket_id"])
                        if args["ticket_id"] != ticket["ticket_id"]:
                            raise ValueError("工具访问了其他工单")
                    self.context.tools.validate(step.tool_name, args, state.get("operator_level", 1),
                                                ticket["order_id"], ticket["user_id"])
                elif step.tool_name or step.arguments:
                    raise ValueError("非工具步骤不能携带工具参数")

        payload = await complete_json_validated(
            self.context.llm, PLANNER_SYSTEM, user, self.context.settings.llm_temperature,
            validator=validate, schema=PlanPayload.model_json_schema(), function_name="submit_plan",
        )
        steps = [s.model_dump() for s in PlanPayload.model_validate(payload).steps]
        for step in steps:
            if step["tool_name"] in {"check_after_sale_eligibility", "calculate_compensation"}:
                step["arguments"].setdefault("ticket_id", ticket["ticket_id"])
        mandatory = [
            ("query_order", {"order_id": ticket["order_id"]}),
            ("query_logistics", {"order_id": ticket["order_id"]}),
            ("check_after_sale_eligibility", {"order_id": ticket["order_id"], "ticket_id": ticket["ticket_id"],
                                             "requested_action": ticket["requested_action"]}),
        ]
        missing = [(name, args) for name, args in mandatory
                   if not any(s["tool_name"] == name and s["arguments"] == args for s in steps)]
        reserved = {s["step_id"] for s in steps}
        inserted = []
        for i, (name, args) in enumerate(missing):
            sid = f"required_{i}"
            while sid in reserved:
                sid += "_"
            reserved.add(sid)
            inserted.append(PlanStep(step_id=sid, agent="tool", action=name,
                                     tool_name=name, arguments=args).model_dump())
        steps = inserted + steps
        if len(steps) < 2 or steps[-2]["agent"] != "memory":
            sid = "required_memory"
            while sid in reserved:
                sid += "_"
            steps.insert(-1, PlanStep(step_id=sid, agent="memory", action="归纳证据").model_dump())
        message = make_message(run_id, "planner", "supervisor", "plan", {"steps": steps})
        await self.context.bus.publish(message)
        return {"plan": steps, "plan_cursor": 0, "completed_steps": [],
                "proposal": None, "messages": [message.model_dump()]}
