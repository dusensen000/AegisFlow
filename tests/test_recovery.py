import asyncio
from fastapi.testclient import TestClient
from app.main import create_app
from conftest import ScenarioLLM, start, finish


class SlowMemoryLLM(ScenarioLLM):
    async def complete(self, system, user, **kwargs):
        if "Memory" in system:
            await asyncio.sleep(30)
        return await super().complete(system, user, **kwargs)


def wait_for_tools(client, context, run_id):
    async def wait():
        for _ in range(200):
            row = await context.repository.run(run_id)
            state = await context.store.get(row["session_id"])
            if state.get("tool_call_count", 0) >= 4:
                return
            await asyncio.sleep(0.02)
        raise AssertionError("Tool queries did not complete")
    client.portal.call(wait)


def test_restart_resumes_checkpoint_without_repeating_completed_tools(context):
    context.llm = SlowMemoryLLM()
    app = create_app(context)
    with TestClient(app) as client:
        client.post("/v1/auth/session", json={"api_token": "staff-a"})
        cookie = client.cookies.get("aegis_session")
        run = start(client)
        wait_for_tools(client, context, run["run_id"])
        conflicting = client.post("/v1/runs", json={"ticket_id": "T20260203002", "thread_id": run["thread_id"], "idempotency_key": "conflict"})
        assert conflicting.status_code == 409
    context.llm = ScenarioLLM()
    with TestClient(create_app(context)) as restarted:
        restarted.cookies.set("aegis_session", cookie)
        assert restarted.get("/v1/runs/" + run["run_id"]).json()["status"] == "paused"
        assert restarted.post("/v1/runs/" + run["run_id"] + "/resume").status_code == 202
        row, events = finish(restarted, run)
        assert row["status"] == "completed"
        assert row["final"]["tool_call_count"] == 4
        assert row["final"]["step_count"] == 7
        assert events.count('"stage": "tool_agent"') == 4


def test_cancel_does_not_turn_disconnect_into_another_run(client, context):
    context.llm = SlowMemoryLLM()
    run = start(client)
    wait_for_tools(client, context, run["run_id"])
    assert client.post("/v1/runs/" + run["run_id"] + "/cancel").json()["status"] == "cancelled"
    row, events = finish(client, run)
    assert row["status"] == "cancelled" and "event: done" in events
    assert client.post("/v1/runs/" + run["run_id"] + "/resume").status_code == 409
