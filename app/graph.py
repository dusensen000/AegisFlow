"""Supervisor-worker graph with per-run checkpoints and bounded budgets."""

from __future__ import annotations

import uuid
import asyncio
import logging
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.agents.memory_agent import MemoryAgent
from app.agents.planner import PlannerAgent
from app.agents.reviewer import ReviewerAgent
from app.agents.supervisor import SupervisorAgent, TERMINAL
from app.agents.tool_agent import ToolAgent
from app.data.seed import get_logistics, get_order, get_rules
from app.models import AgentMemory, Ticket
from app.state import AgentState
from app.llm.client import LLMGenerationError
from app.services.ticket_context import CURRENT_TICKET


def build_graph(context, checkpointer=None):
    graph = StateGraph(AgentState)
    agents = {"supervisor": SupervisorAgent(context), "planner": PlannerAgent(context),
              "tool_agent": ToolAgent(context), "memory_agent": MemoryAgent(context),
              "reviewer": ReviewerAgent(context)}
    for name, agent in agents.items():
        graph.add_node(name, _node(context, name, agent))
    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges("supervisor", lambda state: state["next_agent"],
                                {**{name: name for name in agents if name != "supervisor"}, "end": END})
    for name in agents:
        if name != "supervisor":
            graph.add_edge(name, "supervisor")
    return graph.compile(checkpointer=checkpointer or InMemorySaver())


def _node(context, name, agent):
    async def wrapped(state: AgentState):
        ticket_token = CURRENT_TICKET.set(Ticket.model_validate(state["ticket"]))
        span = context.tracer.start_span(name, thread_id=state["thread_id"],
                                         run_id=state["run_id"])
        try:
            update = await agent.run(dict(state))
            if name != "supervisor":
                update.update(last_worker=name, step_count=state.get("step_count", 0) + 1)
            final = {**state, **update}
            await context.store.set(state["thread_id"], final)
            context.tracer.end_span(span, status=final["status"])
            return update
        except asyncio.CancelledError:
            context.tracer.end_span(span, status="cancelled")
            raise
        except Exception as exc:
            logging.getLogger(__name__).exception("Worker %s failed for run %s", name, state["run_id"])
            context.tracer.end_span(span, status="failed", error=type(exc).__name__)
            manual = isinstance(exc, LLMGenerationError)
            update = {"status": "manual_review" if manual else "failed", "next_agent": "end",
                      "errors": state.get("errors", []) + [f"{name}: {type(exc).__name__}"]}
            if manual:
                update["review"] = {"passed": False, "issues": ["模型生成或结构校验失败，需人工接管"],
                                    "needs_manual_review": True, "needs_replan": False}
            await context.store.set(state["thread_id"], {**state, **update})
            return update
        finally:
            CURRENT_TICKET.reset(ticket_token)
    return wrapped


def build_initial_state(context, ticket: Ticket, thread_id: str, run_id=None,
                        actor_id="internal", operator_level=1, supplements=None, memory=None):
    order = get_order(ticket.order_id)
    logistics = get_logistics(ticket.order_id)
    return {
        "thread_id": thread_id, "run_id": run_id or uuid.uuid4().hex,
        "actor_id": actor_id, "operator_level": operator_level,
        "ticket": ticket.model_dump(mode="json"),
        "order": order.model_dump(mode="json") if order else None,
        "logistics": logistics.model_dump(mode="json") if logistics else None,
        "rules": [rule.model_dump() for rule in get_rules()],
        "plan": [], "plan_cursor": 0, "completed_steps": [],
        "tool_results": [], "memory": memory or AgentMemory().model_dump(),
        "proposal": None, "review": None, "replan_count": 0,
        "step_count": 0, "tool_call_count": 0, "max_steps": context.settings.max_steps,
        "status": "running", "next_agent": "planner", "last_worker": "",
        "errors": [], "review_issues": [], "messages": [], "trace": [],
        "supplements": (supplements or [])[-5:], "retrieved_rules": [],
    }


def graph_config(state):
    return {"configurable": {"thread_id": state["run_id"]},
            "recursion_limit": 2 * state["max_steps"] + 8}


async def run_agent(context, ticket, thread_id, **kwargs):
    initial = build_initial_state(context, ticket, thread_id, **kwargs)
    final = await context.graph.ainvoke(initial, graph_config(initial))
    await context.store.set(thread_id, final)
    return final


async def stream_agent(context, ticket, thread_id, *, initial=None, resume=False, **kwargs):
    initial = initial or build_initial_state(context, ticket, thread_id, **kwargs)
    config = graph_config(initial)
    async for update in context.graph.astream(None if resume else initial, config, stream_mode="updates"):
        for stage, delta in update.items():
            yield {"stage": stage, "delta": delta}
    snapshot = await context.graph.aget_state(config)
    final = dict(snapshot.values)
    if final.get("status") not in TERMINAL:
        final.update(status="failed", errors=final.get("errors", []) + ["执行结束但缺少终态"])
    await context.store.set(thread_id, final)
    yield {"stage": "final", "delta": final}
