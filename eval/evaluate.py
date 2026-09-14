"""Offline regression with optional real Ragas and evidence-aware LLM judging."""

import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict

from app.config import get_settings
from app.context import create_context
from app.data.seed import get_tickets
from app.graph import run_agent
from app.llm.client import complete_json_validated, OpenAICompatClient
from eval.metrics import aggregate_metrics, build_metrics


class JudgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    completeness: float = Field(ge=0, le=1)
    rule_compliance: float = Field(ge=0, le=1)
    hallucination_risk: float = Field(ge=0, le=1)
    issues: list[str]


def factual_context(state):
    return [json.dumps(r["data"], ensure_ascii=False) for r in state.get("tool_results", []) if r["status"] == "ok"] + [
        h["id"] + ": " + h["content"] for h in state.get("retrieved_rules", [])]


class RagasEvaluator:
    def __init__(self, judge_client, model, extra_body=None):
        from ragas.llms import llm_factory
        from ragas.metrics.collections import Faithfulness
        body = extra_body or ({"enable_thinking": False} if model.lower().startswith("qwen") else {})
        self.scorer = Faithfulness(llm=llm_factory(model, client=judge_client, temperature=0, extra_body=body))

    async def evaluate(self, state):
        if not state.get("proposal"):
            return {"enabled": True, "faithfulness": None, "reason": "no_proposal"}
        proposal = state["proposal"]
        response = proposal["summary"] + "\n" + "\n".join(
            f"{i['action']}: {i['reason']}; amount={i.get('amount')}; rules={i['rule_refs']}" for i in proposal["items"])
        score = await self.scorer.ascore(user_input=json.dumps(state["ticket"], ensure_ascii=False),
            response=response, retrieved_contexts=factual_context(state))
        value = float(score.value)
        return {"enabled": True, "faithfulness": value if math.isfinite(value) else None}


class WorkflowEvaluator:
    def __init__(self, context, judge=None, ragas=None):
        self.context, self.judge, self.ragas = context, judge, ragas

    async def evaluate(self, state):
        metrics = build_metrics(state.get("proposal"), ["query_order", "query_logistics", "check_after_sale_eligibility"],
            {r["rule_id"] for r in state.get("rules", [])}, state.get("review"),
            state.get("step_count", 0), state.get("replan_count", 0))
        metrics["llm_judge"] = {"enabled": False, "reason": "not_requested"}
        metrics["ragas"] = {"enabled": False, "reason": "not_requested"}
        if self.judge:
            payload = await complete_json_validated(self.judge,
                "你是独立售后评审员。依据原始事实、规则适用条件和方案评估完整性、合规性和幻觉风险。"
                "不要把生成器的审核结论当作参考答案。返回三个 0-1 评分及 issues。",
                json.dumps({"ticket": state["ticket"], "proposal": state.get("proposal"), "original_context": factual_context(state)}, ensure_ascii=False),
                temperature=0, validator=JudgeResult.model_validate,
                schema=JudgeResult.model_json_schema(), function_name="submit_judgement")
            metrics["llm_judge"] = {"enabled": True, **JudgeResult.model_validate(payload).model_dump()}
        if self.ragas:
            metrics["ragas"] = await self.ragas.evaluate(state)
        metrics.update(ticket_id=state["ticket"]["ticket_id"], status=state["status"])
        return metrics


async def run_evaluation(limit=None, *, judge=False, ragas=False, context=None, output=None):
    context = context or create_context()
    settings = context.settings
    from app.main import create_app
    judge_llm = None
    ragas_client = None
    try:
        if judge:
            judge_llm = OpenAICompatClient(settings.openai_api_key, settings.openai_base_url,
                settings.judge_model or settings.llm_model, 0, settings.llm_extra_body or None, settings.llm_timeout_seconds)
            judge_llm.tracer = context.tracer
        ragas_evaluator = None
        if ragas:
            from openai import AsyncOpenAI
            ragas_client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url, timeout=settings.llm_timeout_seconds, max_retries=0)
            ragas_evaluator = RagasEvaluator(ragas_client, settings.judge_model or settings.llm_model, settings.llm_extra_body)
        evaluator = WorkflowEvaluator(context, judge_llm, ragas_evaluator)
        cases = []
        app = create_app(context)
        async with app.router.lifespan_context(app):
            principal = {"actor_id": "evaluation", "tenant": "evaluation", "level": 1}
            for ticket in get_tickets()[:limit]:
                sid = await context.repository.create_session(principal, ticket.ticket_id)
                run = await app.state.runs.start(await context.repository.session(sid), principal, ticket.ticket_id, "evaluation")
                task = app.state.runs.tasks.get(run["id"])
                if task:
                    await task
                final = (await context.repository.run(run["id"]))["final"]
                cases.append(await evaluator.evaluate(final))
        sources = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in Path("app/agents").glob("*.py")}
        report = {"schema_version": 2, "model": settings.llm_model, "prompt_sources": sources,
                  "case_count": len(cases), "summary": aggregate_metrics(cases), "cases": cases,
                  "notes": "Reference validity and evidence coverage are diagnostics, not overall semantic quality."}
        path = Path(output or settings.eval_report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        return report
    finally:
        if judge_llm:
            await judge_llm.aclose()
        if ragas_client:
            await ragas_client.close()


def main():
    parser = argparse.ArgumentParser(description="AegisFlow workflow regression")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--ragas", action="store_true")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    report = asyncio.run(run_evaluation(args.limit, judge=args.judge, ragas=args.ragas, output=args.output))
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
