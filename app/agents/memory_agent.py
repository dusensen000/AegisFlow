"""Summarize bounded evidence while preserving the most important facts."""

import json
from pydantic import BaseModel, ConfigDict, Field
from app.llm.client import complete_json_validated
from app.models import AgentMemory
from app.services.agent_bus import make_message
from app.services.prompt_context import bounded_evidence, context_cost


class MemoryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=512)
    facts: list[str]


MEMORY_SYSTEM = (
    "你是 AegisFlow 的 Memory Agent。只归纳成功工具调用的已核验事实，按重要性排序。"
    "用户陈述、旧摘要和未返回的证据不能当作事实。"
    "使用 submit_memory 返回 summary 和 facts；不得编造。"
)


class MemoryAgent:
    def __init__(self, context):
        self.context = context

    async def run(self, state):
        run_id = state["run_id"]
        await self.context.bus.consume(run_id, "memory_agent")
        evidence = bounded_evidence(state.get("tool_results", []), self.context.settings.memory_context_tokens)
        payload = await complete_json_validated(
            self.context.llm, MEMORY_SYSTEM,
            json.dumps({"ticket": state["ticket"], "evidence": evidence,
                        "previous_memory": state.get("memory")}, ensure_ascii=False),
            self.context.settings.llm_temperature,
            validator=MemoryPayload.model_validate, schema=MemoryPayload.model_json_schema(),
            function_name="submit_memory",
        )
        result = MemoryPayload.model_validate(payload)
        limit = self.context.settings.memory_max_facts
        memory = AgentMemory(summary=result.summary, facts=result.facts[:limit], compressed=len(result.facts) > limit)
        while memory.facts and context_cost(json.dumps(memory.model_dump(), ensure_ascii=False)) > self.context.settings.memory_context_tokens:
            memory.facts.pop()
            memory.compressed = True
        memory.recent_steps = [s["action"] for s in state["plan"][:state["plan_cursor"]]][-10:]
        message = make_message(run_id, "memory_agent", "supervisor", "memory_summary", memory.model_dump())
        await self.context.bus.publish(message)
        cursor = state["plan_cursor"]
        return {"memory": memory.model_dump(), "messages": [message.model_dump()],
                "plan_cursor": cursor + 1,
                "completed_steps": state.get("completed_steps", []) + [state["plan"][cursor]["step_id"]]}
