"""LangGraph 共享状态定义。"""

from typing import TypedDict


class AgentState(TypedDict, total=False):
    run_id: str
    actor_id: str
    operator_level: int
    next_agent: str
    last_worker: str
    plan_cursor: int
    completed_steps: list[str]
    tool_call_count: int
    supplements: list[str]
    retrieved_rules: list[dict]
    thread_id: str
    ticket: dict
    order: dict | None
    logistics: dict | None
    rules: list[dict]
    plan: list[dict]
    tool_results: list[dict]
    memory: dict
    proposal: dict | None
    review: dict | None
    replan_count: int
    step_count: int
    max_steps: int
    status: str
    errors: list[str]
    review_issues: list[str]
    messages: list[dict]
    trace: list[dict]
