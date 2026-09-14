"""Owned staff sessions, idempotent runs, SSE replay and manual-review queue."""

import asyncio
import json
from fastapi import APIRouter, HTTPException, Request, Response, Query
from fastapi.responses import StreamingResponse

from app.api.schemas import (CreateSessionResponse, RunAgentRequest, RunAgentResponse,
                             AuthRequest, SessionRequest, SupplementRequest, DecisionRequest, FeedbackRequest,
                             TicketCreateRequest, TicketEditRequest, ConversationRequest, ChatMessageRequest)
from app.data.seed import get_ticket, get_tickets, get_rules, get_orders
from app.services.auth import AuthService
from app.services.memory_store import InMemoryRateLimiter, RedisRateLimiter
from app.services.runs import TERMINAL


def build_router(context):
    router = APIRouter()
    settings, repo = context.settings, context.repository
    auth = AuthService(settings, repo)
    limiter = (RedisRateLimiter(settings.redis_url, settings.rate_limit_per_minute)
               if settings.use_redis else InMemoryRateLimiter(settings.rate_limit_per_minute))

    async def limit(request, principal=None):
        ip = request.client.host if request.client else "internal"
        if not await limiter.allow("ip:" + ip) or (principal and not await limiter.allow("actor:" + principal["actor_id"])):
            raise HTTPException(429, "请求过于频繁")

    async def own_run(request, rid):
        row = await repo.run(rid)
        if not row:
            raise HTTPException(404, "任务不存在")
        principal, session = await auth.require_session(request, row["session_id"])
        await auth.require_ticket(principal, row["ticket_id"])
        return row, principal, session

    async def start_run(request, body):
        principal = await auth.principal(request)
        await auth.require_ticket(principal, body.ticket_id)
        await limit(request, principal)
        sid = body.thread_id or await repo.create_session(principal, body.ticket_id)
        _, session = await auth.require_session(request, sid)
        try:
            row = await request.app.state.runs.start(session, principal, body.ticket_id, body.idempotency_key)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return row

    @router.get("/health")
    async def health():
        return {"status": "ok", "service": settings.app_name}

    @router.post("/v1/auth/session")
    async def authenticate(request: Request, response: Response, body: AuthRequest = AuthRequest()):
        await limit(request)
        principal = await auth.bootstrap(request, response, body.api_token)
        return {"actor_id": principal["actor_id"], "level": principal["level"], "tenant": principal["tenant"]}

    @router.post("/v1/sessions", response_model=CreateSessionResponse)
    async def create_session(request: Request, body: SessionRequest = SessionRequest()):
        principal = await auth.principal(request)
        await limit(request, principal)
        if body.ticket_id:
            await auth.require_ticket(principal, body.ticket_id)
        return CreateSessionResponse(thread_id=await repo.create_session(principal, body.ticket_id))

    @router.get("/v1/sessions")
    async def list_sessions(request: Request):
        principal = await auth.principal(request)
        rows = await repo.list_sessions(principal)
        return {"sessions": [row for row in rows if not row["ticket_id"] or await context.catalog.get(principal, row["ticket_id"])]}

    @router.get("/v1/sessions/{thread_id}")
    async def get_session(thread_id: str, request: Request):
        _, session = await auth.require_session(request, thread_id)
        return {**session, "runs": await repo.session_runs(thread_id)}

    @router.post("/v1/sessions/{thread_id}/supplements")
    async def supplement(thread_id: str, request: Request, body: SupplementRequest):
        principal, _ = await auth.require_session(request, thread_id)
        await limit(request, principal)
        if not body.text.strip():
            raise HTTPException(422, "补充材料不能为空")
        try:
            await repo.supplement(thread_id, body.text.strip())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "saved"}

    @router.get("/v1/sessions/{thread_id}/messages")
    async def messages(thread_id: str, request: Request):
        await auth.require_session(request, thread_id)
        runs = await repo.session_runs(thread_id)
        output = []
        for row in runs:
            for event in await repo.events(row["id"]):
                output.extend(event["data"].get("delta", {}).get("messages", []))
        return {"messages": output}

    @router.post("/v1/runs", status_code=202)
    async def create_run(request: Request, body: RunAgentRequest):
        row = await start_run(request, body)
        return {"run_id": row["id"], "thread_id": row["session_id"], "status": row["status"],
                "events_url": f"/v1/runs/{row['id']}/events"}

    @router.post("/v1/agents/run", response_model=RunAgentResponse)
    async def run(request: Request, body: RunAgentRequest):
        row = await start_run(request, body)
        task = request.app.state.runs.tasks.get(row["id"])
        if task:
            await asyncio.shield(task)
        row = await repo.run(row["id"])
        final = row["final"] or {}
        return RunAgentResponse(thread_id=row["session_id"], status=row["status"],
            proposal=final.get("proposal"), review=final.get("review"),
            errors=final.get("errors", []), step_count=final.get("step_count", 0))

    @router.get("/v1/runs/{run_id}")
    async def get_run(run_id: str, request: Request):
        row, _, _ = await own_run(request, run_id)
        feedback = await repo.one("SELECT data FROM feedback WHERE run_id=?", (run_id,))
        manual = await repo.one("SELECT status,assignee,decision FROM manual_reviews WHERE run_id=?", (run_id,))
        if manual and manual["decision"]:
            manual["decision"] = json.loads(manual["decision"])
        return {**row, "feedback": json.loads(feedback["data"]) if feedback else None, "manual_review": manual}

    @router.post("/v1/runs/{run_id}/feedback")
    async def feedback(run_id: str, request: Request, body: FeedbackRequest):
        row, principal, _ = await own_run(request, run_id)
        await limit(request, principal)
        if row["status"] not in TERMINAL:
            raise HTTPException(409, "任务尚未完成")
        if body.outcome == "accepted" and (row["status"] != "completed" or not (row["final"] or {}).get("review", {}).get("passed")):
            raise HTTPException(409, "未通过审核的建议不能采纳")
        await repo.save_feedback(run_id, principal["actor_id"], body.model_dump())
        return {"status": "saved"}

    @router.post("/v1/runs/{run_id}/cancel")
    async def cancel(run_id: str, request: Request):
        row, principal, _ = await own_run(request, run_id)
        await limit(request, principal)
        await request.app.state.runs.cancel(row)
        return {"status": (await repo.run(run_id))["status"]}

    @router.post("/v1/runs/{run_id}/resume", status_code=202)
    async def resume(run_id: str, request: Request):
        row, principal, _ = await own_run(request, run_id)
        await limit(request, principal)
        if row["status"] != "paused":
            raise HTTPException(409, "只有暂停任务可恢复")
        row["initial"]["operator_level"] = principal["level"]
        try:
            request.app.state.runs.launch(row, resume=True)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"run_id": run_id, "status": "running"}

    def sse_response(row, request, after):
        async def source():
            cursor = after
            heartbeat = 0
            while True:
                events = await repo.events(row["id"], cursor)
                for event in events:
                    cursor = event["seq"]
                    yield _sse(event["type"], {**event["data"], "created": event["created"]}, cursor)
                current = await repo.run(row["id"])
                if current["status"] in TERMINAL or current["status"] == "paused":
                    # Fetch again after the terminal status read to cover the final-event race.
                    for event in await repo.events(row["id"], cursor):
                        yield _sse(event["type"], {**event["data"], "created": event["created"]}, event["seq"])
                    break
                if await request.is_disconnected():
                    break
                await asyncio.sleep(0.2)
                heartbeat += 1
                if heartbeat >= 50:
                    yield ": heartbeat\n\n"
                    heartbeat = 0
        return StreamingResponse(source(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.get("/v1/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after: int = Query(default=0, ge=0)):
        row, _, _ = await own_run(request, run_id)
        try:
            cursor = int(request.headers.get("last-event-id", after))
        except ValueError as exc:
            raise HTTPException(400, "无效的事件游标") from exc
        last = await repo.one("SELECT COALESCE(MAX(seq),0) AS seq FROM events WHERE run_id=?", (run_id,))
        if cursor < 0 or cursor > last["seq"]:
            raise HTTPException(400, "事件游标超出范围")
        return sse_response(row, request, cursor)

    @router.get("/v1/agents/{thread_id}/stream")
    async def legacy_stream(thread_id: str, request: Request, ticket_id: str):
        _, session = await auth.require_session(request, thread_id)
        rows = await repo.session_runs(thread_id)
        if not rows or session["ticket_id"] != ticket_id:
            raise HTTPException(409, "请先 POST /v1/runs 启动任务")
        return sse_response(await repo.run(rows[0]["id"]), request, 0)

    @router.get("/v1/tools")
    async def tools(request: Request):
        principal = await auth.principal(request)
        return {"tools": [t for t in context.tools.list_tools() if t["permissionLevel"] <= principal["level"]]}

    @router.get("/v1/orders")
    async def orders(request: Request):
        allowed = await context.catalog.allowed_orders(await auth.principal(request))
        return {"orders": [order.model_dump(mode="json") for order in get_orders() if order.order_id in allowed]}

    @router.get("/v1/tickets")
    async def tickets(request: Request, deleted: bool = False):
        return {"tickets": await context.catalog.list(await auth.principal(request), deleted=deleted)}

    @router.post("/v1/tickets", status_code=201)
    async def create_ticket(request: Request, body: TicketCreateRequest):
        principal = await auth.principal(request)
        await limit(request, principal)
        try:
            ticket = await context.catalog.create(principal, **body.model_dump())
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {**ticket.model_dump(mode="json"), "source": "manual", "editable": True}

    async def change_ticket(request, ticket_id, **options):
        principal = await auth.principal(request)
        await limit(request, principal)
        try:
            ticket = await context.catalog.change(principal, ticket_id, **options)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return ticket.model_dump(mode="json")

    @router.patch("/v1/tickets/{ticket_id}")
    async def edit_ticket(ticket_id: str, request: Request, body: TicketEditRequest):
        fields = body.model_dump(exclude_unset=True)
        if not fields or any(value is None for value in fields.values()):
            raise HTTPException(422, "请提供有效的修改字段")
        return await change_ticket(request, ticket_id, fields=fields)

    @router.delete("/v1/tickets/{ticket_id}")
    async def delete_ticket(ticket_id: str, request: Request):
        await change_ticket(request, ticket_id, delete=True)
        return {"status": "deleted"}

    @router.post("/v1/tickets/{ticket_id}/restore")
    async def restore_ticket(ticket_id: str, request: Request):
        return await change_ticket(request, ticket_id, delete=False)

    @router.get("/v1/rules")
    async def rules(request: Request):
        await auth.principal(request)
        return {"rules": [r.model_dump() for r in get_rules()]}

    async def chat_call(request, method, *args, write=False):
        principal = await auth.principal(request)
        if write:
            await limit(request, principal)
        try:
            return await getattr(request.app.state.chat, method)(principal, *args)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/v1/conversations", status_code=201)
    async def create_conversation(request: Request, body: ConversationRequest = ConversationRequest()):
        return await chat_call(request, "create", body.order_id, write=True)

    @router.get("/v1/conversations")
    async def conversations(request: Request):
        return {"conversations": await chat_call(request, "list")}

    @router.get("/v1/conversations/{conversation_id}")
    async def conversation(conversation_id: str, request: Request):
        return await chat_call(request, "detail", conversation_id)

    @router.post("/v1/conversations/{conversation_id}/messages", status_code=202)
    async def send_message(conversation_id: str, request: Request, body: ChatMessageRequest):
        return await chat_call(request, "submit", conversation_id, body, write=True)

    @router.post("/v1/conversations/{conversation_id}/cancel")
    async def cancel_conversation(conversation_id: str, request: Request):
        await chat_call(request, "cancel", conversation_id, write=True)
        return {"status": "cancelled"}

    @router.post("/v1/conversations/{conversation_id}/requests/{request_id}/retry", status_code=202)
    async def retry_message(conversation_id: str, request_id: str, request: Request):
        return await chat_call(request, "retry", conversation_id, request_id, write=True)

    @router.delete("/v1/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str, request: Request):
        await chat_call(request, "delete", conversation_id, write=True)
        return {"status": "deleted"}

    @router.get("/v1/conversations/{conversation_id}/events")
    async def conversation_events(conversation_id: str, request: Request, after: int = Query(default=0, ge=0)):
        await chat_call(request, "own", conversation_id)
        principal = await auth.principal(request)
        try:
            cursor = int(request.headers.get("last-event-id", after))
        except ValueError as exc:
            raise HTTPException(400, "无效事件游标") from exc
        last = await repo.one("SELECT COALESCE(MAX(seq),0) AS seq FROM chat_events WHERE conversation_id=?", (conversation_id,))
        if cursor < 0 or cursor > last["seq"]:
            raise HTTPException(400, "事件游标超出范围")
        async def source():
            nonlocal cursor
            heartbeat = 0
            while not await request.is_disconnected():
                try:
                    rows = await request.app.state.chat.events(principal, conversation_id, cursor)
                except (LookupError, PermissionError):
                    break
                for event in rows:
                    cursor = event["seq"]
                    yield _sse(event["type"], {**event["data"], "created": event["created"]}, cursor)
                await asyncio.sleep(0.2)
                heartbeat += 1
                if heartbeat >= 50:
                    yield ": heartbeat\n\n"
                    heartbeat = 0
        return StreamingResponse(source(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.get("/v1/manual-reviews")
    async def manual_reviews(request: Request):
        principal = await auth.principal(request)
        rows = await repo.manual_list(principal)
        rows = [row for row in rows if row["status"] != "withdrawn" and
                await context.catalog.record(principal, row["ticket_id"], team=principal["level"] >= 3)]
        for row in rows:
            row["final"] = json.loads(row["final"]) if row["final"] else None
            row["decision"] = json.loads(row["decision"]) if row["decision"] else None
        return {"items": rows, "can_review": principal["level"] >= 3}

    async def reviewer(request):
        principal = await auth.principal(request)
        if principal["level"] < 3:
            raise HTTPException(403, "需要高级客服权限")
        await limit(request, principal)
        return principal

    async def review_scope(request, run_id):
        principal = await reviewer(request)
        row = await repo.run(run_id)
        if not row:
            raise HTTPException(404, "人工任务不存在")
        await auth.require_ticket(principal, row["ticket_id"], team=True)
        return principal

    @router.post("/v1/manual-reviews/{run_id}/claim")
    async def claim(run_id: str, request: Request):
        try:
            await repo.manual_decision(run_id, await review_scope(request, run_id))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "claimed"}

    @router.post("/v1/manual-reviews/{run_id}/decision")
    async def decision(run_id: str, request: Request, body: DecisionRequest):
        try:
            await repo.manual_decision(run_id, await review_scope(request, run_id), body.model_dump())
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "resolved"}

    return router


def _sse(event, data, event_id):
    return f"id: {event_id}\nevent: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
