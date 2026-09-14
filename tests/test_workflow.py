import json
from conftest import start, finish


def test_valid_compensation_and_terminal_state(client):
    run, events = finish(client, start(client))
    assert run["status"] == "completed"
    assert run["final"]["review"]["passed"]
    assert run["final"]["proposal"]["items"][0]["evidence_ids"]
    assert run["final"]["memory"]["facts"] == ["fact " + str(i) for i in range(8)]
    assert "event: done" in events
    assert '"stage": "tool_agent"' in events
    assert client.post("/v1/runs/" + run["id"] + "/feedback", json={"outcome": "accepted"}).status_code == 200


def test_bad_proposal_replans_then_enters_real_queue(client, context):
    context.llm.bad = True
    run, _ = finish(client, start(client, "T20260206004"))
    assert run["status"] == "manual_review"
    assert run["final"]["replan_count"] == 2
    assert not run["final"]["review"]["passed"]
    assert run["final"]["review"]["issues"]
    assert len(client.get("/v1/manual-reviews").json()["items"]) == 1
    assert client.post("/v1/runs/" + run["id"] + "/feedback", json={"outcome": "accepted"}).status_code == 409


def test_execution_budget_checked_before_every_dispatch(client, context):
    context.settings.max_steps = 1
    run, _ = finish(client, start(client))
    assert run["status"] == "manual_review"
    assert run["final"]["step_count"] == 1
    assert run["final"]["tool_call_count"] == 0


def test_idempotency_replay_does_not_rerun_tools(client, context):
    first = start(client)
    row, all_events = finish(client, first)
    second = start(client, sid=first["thread_id"])
    assert first["run_id"] == second["run_id"]
    before = row["final"]["tool_call_count"]
    ids = [int(line[4:]) for line in all_events.splitlines() if line.startswith("id: ")]
    response = client.get(first["events_url"], headers={"Last-Event-ID": str(ids[-2])})
    assert "event: done" in response.text and '"stage": "planner"' not in response.text
    assert client.get("/v1/runs/" + first["run_id"]).json()["final"]["tool_call_count"] == before
    assert client.get(first["events_url"], headers={"Last-Event-ID": "999999"}).status_code == 400


def test_session_owner_and_role_cannot_be_forged(client):
    run, _ = finish(client, start(client))
    assert client.post("/v1/auth/session", json={"api_token": "staff-b"}).status_code == 200
    assert client.get("/v1/runs/" + run["id"]).status_code == 404
    assert client.get("/v1/sessions/" + run["session_id"]).status_code == 404
    assert client.post("/v1/manual-reviews/" + run["id"] + "/claim").status_code == 403
    assert client.post("/v1/runs", json={"ticket_id": "T20260203002", "operator_level": 3}).status_code == 422


def test_manual_review_claim_and_decision(client, context):
    context.llm.bad = True
    run, _ = finish(client, start(client, "T20260206004"))
    client.post("/v1/auth/session", json={"api_token": "admin"})
    endpoint = "/v1/manual-reviews/" + run["id"]
    assert client.post(endpoint + "/claim").status_code == 200
    assert client.post(endpoint + "/claim").status_code == 409
    assert client.post(endpoint + "/decision", json={"outcome": "rejected", "note": "Expired and unsupported amount"}).status_code == 200
    assert client.get("/v1/manual-reviews").json()["items"][0]["status"] == "resolved"
    client.post("/v1/auth/session", json={"api_token": "staff-a"})
    assert client.get("/v1/runs/" + run["id"]).json()["manual_review"]["decision"]["outcome"] == "rejected"


def test_static_assets_and_same_origin_guard(client):
    for path in ("/", "/static/style.css", "/static/app.js", "/static/vendor/lucide.min.js"):
        assert client.get(path).status_code == 200
    assert client.post("/v1/auth/session", headers={"Origin": "https://other-site.example"}).status_code == 403


def test_model_failure_hands_off_instead_of_publishing_a_proposal(client, context):
    async def invalid(*args, **kwargs):
        return "not valid json"
    context.llm.complete = invalid
    run, _ = finish(client, start(client))
    assert run["status"] == "manual_review"
    assert not run["final"]["review"]["passed"]
    assert run["final"]["proposal"] is None
    assert client.get("/v1/manual-reviews").json()["items"]


def test_supplements_persist_and_bind_session_to_ticket(client):
    first = start(client)
    finish(client, first)
    endpoint = "/v1/sessions/" + first["thread_id"]
    assert client.post(endpoint + "/supplements", json={"text": "Please explain the compensation"}).status_code == 200
    second = start(client, sid=first["thread_id"], key="follow-up")
    row, _ = finish(client, second)
    assert row["initial"]["supplements"] == ["Please explain the compensation"]
    assert row["initial"]["memory"]["summary"]
    assert client.post("/v1/runs", json={"ticket_id": "T20260206004", "thread_id": first["thread_id"], "idempotency_key": "other"}).status_code == 409


def test_reviewer_cannot_bypass_assigned_ticket_scope(client, context):
    context.llm.bad = True
    run, _ = finish(client, start(client, "T20260206004"))
    context.settings.api_users["admin"]["allowed_ticket_ids"] = ["T20260203002"]
    client.post("/v1/auth/session", json={"api_token": "admin"})
    assert client.get("/v1/manual-reviews").json()["items"] == []
    endpoint = "/v1/manual-reviews/" + run["id"]
    assert client.post(endpoint + "/claim").status_code == 403
    assert client.post(endpoint + "/decision", json={"outcome": "approved", "note": "unauthorized"}).status_code == 403


def test_latest_query_price_overrides_initial_snapshot(client, context):
    original = context.tools._query_order("SO20260114002")
    context.tools._tools["query_order"].handler = lambda order_id: {**original, "amount": 50}
    run, _ = finish(client, start(client))
    assert run["final"]["order"]["amount"] == 50
    assert not run["final"]["review"]["passed"]
    assert run["status"] == "manual_review"
