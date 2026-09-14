import json
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.context import create_context
from app.llm.base import LLMClient
from app.main import create_app


class ScenarioLLM(LLMClient):
    model = "offline-test"
    bad = False

    async def complete(self, system, user, **kwargs):
        if "Memory" in system:
            return json.dumps({"summary": "Verified tool facts", "facts": ["fact " + str(i) for i in range(12)]})
        data = json.loads(user.split("\n\n")[0])
        ticket = data["ticket"]
        if "Planner" in system:
            steps = [{"step_id": "order", "agent": "tool", "action": "query", "tool_name": "query_order", "arguments": {"order_id": ticket["order_id"]}},
                     {"step_id": "logistics", "agent": "tool", "action": "query", "tool_name": "query_logistics", "arguments": {"order_id": ticket["order_id"]}}]
            if not self.bad:
                steps.append({"step_id": "calc", "agent": "tool", "action": "calculate", "tool_name": "calculate_compensation", "arguments": {"order_id": ticket["order_id"], "ticket_id": ticket["ticket_id"], "damage_level": "low"}})
            steps.append({"step_id": "review", "agent": "reviewer", "action": "review"})
            return json.dumps({"steps": steps})
        item = {"action": "退款" if self.bad else "补偿", "reason": "offline test", "rule_refs": ["R001" if self.bad else "R004"],
                "amount": 999999 if self.bad else 19.95,
                "evidence_tools": ["invented_tool"] if self.bad else ["query_order", "query_logistics", "calculate_compensation"]}
        return json.dumps({"ticket_id": ticket["ticket_id"], "summary": "offline test proposal", "items": [item], "confidence": 0.8})


@pytest.fixture
def context(tmp_path):
    settings = Settings(_env_file=None, openai_api_key="offline-test-key", database_path=str(tmp_path / "app.db"),
        enable_tracing=False, demo_auth=False, max_steps=40, rate_limit_per_minute=10000,
        api_users={"staff-a": {"actor_id": "a", "tenant": "shop", "level": 1},
                   "staff-b": {"actor_id": "b", "tenant": "shop", "level": 1},
                   "admin": {"actor_id": "admin", "tenant": "shop", "level": 3}})
    context = create_context(settings)
    context.llm = ScenarioLLM()
    return context


@pytest.fixture
def client(context):
    with TestClient(create_app(context)) as client:
        assert client.post("/v1/auth/session", json={"api_token": "staff-a"}).status_code == 200
        yield client


def start(client, ticket="T20260203002", sid=None, key="test-key"):
    sid = sid or client.post("/v1/sessions", json={"ticket_id": ticket}).json()["thread_id"]
    response = client.post("/v1/runs", json={"ticket_id": ticket, "thread_id": sid, "idempotency_key": key})
    assert response.status_code == 202, response.text
    return response.json()


def finish(client, run):
    response = client.get(run["events_url"])
    assert response.status_code == 200, response.text
    return client.get("/v1/runs/" + run["run_id"]).json(), response.text
