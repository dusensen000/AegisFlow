"""Persist customer conversations and translate requests into audited ticket runs."""

import asyncio
import json
import re
import time
import uuid
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from app.data.seed import get_order
from app.llm.client import complete_json_validated
from app.services.repository import encode
from app.services.runs import TERMINAL


class Intake(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["after_sale", "question", "greeting"]
    intent: Literal["退款", "退货", "换货", "补偿", "物流异常", "商品破损", "少件", "其他"]
    requested_action: Literal["退款", "退货", "换货", "补偿", "仅退款", "人工复核"] | None
    description: str = Field(min_length=1, max_length=2000)
    user_claim: str = Field(max_length=2000)
    title: str = Field(min_length=1, max_length=40)
    clarification: str = Field(max_length=500)


INTAKE_SYSTEM = (
    "你是售后客服 Intake，归纳顾客对话中的当前诉求。使用 submit_intake 返回结构化结果。"
    "结合前文和已关联订单理解指代；不要把用户陈述当成已核验事实，不承诺退款已经执行。"
    "kind=after_sale 表示新诉求、补充材料或更改诉求；question 表示询问已有处理原因或进展；"
    "greeting 表示问候或致谢。信息不足时 requested_action=null，并用 clarification 追问。"
    "description 应包含本次问题及前文必要信息，不包括助手猜测。"
    "已付款但未发货的取消退款归类为仅退款。title 用简短的售后问题名称。"
)


def customer_result(final):
    status = final.get("status")
    review = final.get("review") or {}
    if status == "completed" and review.get("passed"):
        proposal = final.get("proposal") or {}
        lines = ["已核对您的订单，处理建议如下："]
        order = final.get("order") or {}
        calculation = next((result.get("data", {}) for result in final.get("tool_results", [])
                            if result.get("tool_name") == "calculate_compensation" and result.get("status") == "ok"), {})
        for item in proposal.get("items", []):
            amount = f" ¥{item['amount']:.2f}" if item.get("amount") is not None else ""
            action = item["action"]
            if action == "仅退款":
                lines.append(("订单尚未发货，建议取消订单并仅退款" if order.get("status") == "待发货" else "根据本次核验结果，建议仅退款") + amount + "，无需退回商品。")
            elif action == "退款":
                lines.append("订单符合本次退款条件，建议按原支付渠道退款" + amount + "。")
            elif action == "退货":
                lines.append("建议退货退款" + amount + "，退回商品与寄回安排由客服确认。")
            elif action == "换货":
                lines.append("建议办理换货，换货商品与寄回安排由客服确认。")
            elif action == "补偿":
                basis = "已核验的商品破损" if calculation.get("basis") == "confirmed_damage" else "物流异常"
                lines.append("针对" + basis + "，建议补偿" + amount + "。")
        lines.append("该方案尚未执行退款或赔付，将由客服确认后处理。")
        return "\n\n".join(lines)
    if status == "cancelled":
        return "本次处理已停止，没有执行退款或赔付。"
    issues = list(dict.fromkeys(review.get("issues", []) + final.get("review_issues", [])))
    if status == "manual_review":
        if (final.get("order") or {}).get("protection_expired"):
            reason = "该订单超出自动售后保障范围，需要客服核实具体情况。"
        elif any("高频" in issue or "高级客服" in issue or "大额" in issue for issue in issues):
            reason = "本次售后需要由高级客服进一步复核。"
        else:
            reason = "本次自动核对尚未能确认完整的售后条件，需要客服结合订单时间及相关证明继续核实。"
        return "该诉求已转人工复核，暂未执行退款或赔付。\n\n" + reason
    return "暂时无法完成本次核对，请稍后重试。没有执行退款或赔付。"


class ChatService:
    def __init__(self, context, runs):
        self.context, self.repo, self.runs = context, context.repository, runs
        self.tasks = {}
        self.cancelling = set()

    async def own(self, principal, cid):
        row = await self.repo.one("SELECT * FROM conversations WHERE id=? AND owner=? AND tenant=? AND deleted=0",
            (cid, principal["actor_id"], principal["tenant"]))
        if not row:
            raise LookupError("会话不存在或已删除")
        if row["order_id"] and row["order_id"] not in await self.context.catalog.allowed_orders(principal):
            raise PermissionError("无权访问会话关联的订单")
        return row

    async def create(self, principal, order_id=None):
        if order_id and order_id not in await self.context.catalog.allowed_orders(principal):
            raise PermissionError("订单不存在或无访问权限")
        cid = uuid.uuid4().hex
        async with self.repo.lock:
            await self.repo.conn.execute("INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?)",
                (cid, principal["actor_id"], principal["tenant"], "新售后会话", order_id, 0, time.time(), time.time()))
        return await self.own(principal, cid)

    async def list(self, principal):
        rows = await self.repo.all("SELECT * FROM conversations WHERE owner=? AND tenant=? AND deleted=0 ORDER BY updated DESC LIMIT 100",
            (principal["actor_id"], principal["tenant"]))
        allowed = await self.context.catalog.allowed_orders(principal)
        for row in rows:
            row["request"] = await self.repo.one("SELECT id,status,ticket_id,session_id,run_id FROM chat_requests WHERE conversation_id=? ORDER BY created DESC LIMIT 1", (row["id"],))
        return [row for row in rows if not row["order_id"] or row["order_id"] in allowed]

    async def detail(self, principal, cid):
        row = await self.own(principal, cid)
        messages = await self.repo.all("SELECT * FROM chat_messages WHERE conversation_id=? ORDER BY created,id", (cid,))
        for message in messages:
            message["data"] = json.loads(message["data"])
        requests = await self.repo.all("SELECT id,status,ticket_id,session_id,run_id,request_key FROM chat_requests WHERE conversation_id=? ORDER BY created DESC", (cid,))
        for request in requests:
            run = await self.repo.one("SELECT status FROM runs WHERE id=?", (request["run_id"],)) if request["run_id"] else None
            request["result_status"] = run["status"] if run else None
        return {**row, "messages": messages, "requests": requests}

    async def event_locked(self, cid, event_type, data):
        row = await self.repo.one("SELECT COALESCE(MAX(seq),0)+1 AS seq FROM chat_events WHERE conversation_id=?", (cid,))
        await self.repo.conn.execute("INSERT INTO chat_events VALUES(?,?,?,?,?)", (cid, row["seq"], event_type, encode(data), time.time()))

    async def message_locked(self, cid, reqid, role, content, data=None):
        mid, created = uuid.uuid4().hex, time.time()
        cursor = await self.repo.conn.execute("INSERT OR IGNORE INTO chat_messages VALUES(?,?,?,?,?,?,?)",
            (mid, cid, reqid, role, content, encode(data or {}), created))
        if cursor.rowcount:
            await self.event_locked(cid, "message", {"id": mid, "role": role, "content": content, "data": data or {}, "created": created})

    async def status(self, row, status, **links):
        async with self.repo.lock:
            await self.repo.conn.execute("BEGIN IMMEDIATE")
            try:
                await self.repo.conn.execute("UPDATE chat_requests SET status=?,ticket_id=COALESCE(?,ticket_id),session_id=COALESCE(?,session_id),run_id=COALESCE(?,run_id) WHERE id=?",
                    (status, links.get("ticket_id"), links.get("session_id"), links.get("run_id"), row["id"]))
                await self.event_locked(row["conversation_id"], "status", {"request_id": row["id"], "status": status, **links})
                await self.repo.conn.commit()
            except BaseException:
                await self.repo.conn.rollback()
                raise

    async def reply(self, row, text, status="completed", **links):
        async with self.repo.lock:
            await self.repo.conn.execute("BEGIN IMMEDIATE")
            try:
                await self.message_locked(row["conversation_id"], row["id"], "assistant", text, links)
                await self.repo.conn.execute("UPDATE chat_requests SET status=? WHERE id=?", (status, row["id"]))
                await self.event_locked(row["conversation_id"], "status", {"request_id": row["id"], "status": status, **links})
                await self.repo.conn.execute("UPDATE conversations SET updated=? WHERE id=?", (time.time(), row["conversation_id"]))
                await self.repo.conn.commit()
            except BaseException:
                await self.repo.conn.rollback()
                raise

    async def submit(self, principal, cid, body):
        conversation = await self.own(principal, cid)
        if body.order_id and body.order_id not in await self.context.catalog.allowed_orders(principal):
            raise PermissionError("订单不存在或无访问权限")
        if body.order_id and conversation["order_id"] not in (None, body.order_id):
            raise ValueError("会话已关联另一笔订单，请为其他订单新建会话")
        payload = {"text": body.text, "order_id": body.order_id}
        launch = False
        async with self.repo.lock:
            await self.repo.conn.execute("BEGIN IMMEDIATE")
            try:
                row = await self.repo.one("SELECT * FROM chat_requests WHERE conversation_id=? AND request_key=?", (cid, body.idempotency_key))
                if row and json.loads(row["payload"]) != payload:
                    raise ValueError("消息幂等键已用于不同内容")
                if not row or row["status"] == "interrupted":
                    active = await self.repo.one("SELECT id FROM chat_requests WHERE conversation_id=? AND status IN ('pending','processing')", (cid,))
                    if active:
                        raise ValueError("当前诉求正在处理，请等待结果或先停止处理")
                    if row:
                        await self.repo.conn.execute("UPDATE chat_requests SET status='pending' WHERE id=?", (row["id"],))
                        row["status"] = "pending"
                    else:
                        rid = uuid.uuid4().hex
                        await self.repo.conn.execute("INSERT INTO chat_requests VALUES(?,?,?,?,?,?,?,?,?)",
                            (rid, cid, body.idempotency_key, encode(payload), "pending", None, None, None, time.time()))
                        row = await self.repo.one("SELECT * FROM chat_requests WHERE id=?", (rid,))
                        await self.message_locked(cid, rid, "user", body.text, {"request_key": body.idempotency_key})
                    await self.event_locked(cid, "status", {"request_id": row["id"], "status": "pending"})
                    launch = True
                if body.order_id and not conversation["order_id"]:
                    await self.repo.conn.execute("UPDATE conversations SET order_id=? WHERE id=?", (body.order_id, cid))
                await self.repo.conn.commit()
            except BaseException:
                await self.repo.conn.rollback()
                raise
        if launch:
            task = asyncio.create_task(self.process(row, principal), name="chat-" + row["id"])
            self.tasks[row["id"]] = task
            task.add_done_callback(lambda completed: self.tasks.pop(row["id"], None))
        return {"request_id": row["id"], "status": row["status"]}

    async def process(self, row, principal):
        try:
            await self.status(row, "processing")
            conversation = await self.own(principal, row["conversation_id"])
            payload = json.loads(row["payload"])
            if row["run_id"]:
                run = await self.repo.run(row["run_id"])
                if run["status"] == "paused":
                    run["initial"]["operator_level"] = principal["level"]
                    self.runs.launch(run, resume=True)
            else:
                text = payload["text"]
                if text.strip("！？!?。 .").lower() in {"你好", "您好", "在吗", "hello", "谢谢", "好的"}:
                    await self.reply(row, "您好，请告诉我需要处理的订单和售后问题。" if "谢" not in text else "不客气，后续有需要可继续补充诉求。")
                    return
                allowed = await self.context.catalog.allowed_orders(principal)
                mentioned = [oid for oid in allowed if re.search(r"(?<![A-Za-z0-9])" + re.escape(oid) + r"(?![A-Za-z0-9])", text, re.I)]
                identifiers = re.findall(r"SO[A-Z0-9]{6,}", text.upper())
                if any(identifier not in allowed for identifier in identifiers):
                    await self.reply(row, "暂未找到该订单或没有访问权限，请核对订单号。")
                    return
                order_id = payload["order_id"] or conversation["order_id"] or (mentioned[0] if len(mentioned) == 1 else None)
                if len(mentioned) > 1 or (mentioned and order_id not in mentioned):
                    await self.reply(row, "请确认本次需要处理的是哪一笔订单。")
                    return
                if not order_id:
                    await self.reply(row, "请提供订单号，方便核对订单、物流和售后资格。")
                    return
                order = get_order(order_id)
                async with self.repo.lock:
                    await self.repo.conn.execute("UPDATE conversations SET order_id=? WHERE id=?", (order_id, conversation["id"]))
                history = await self.repo.all("SELECT role,content FROM chat_messages WHERE conversation_id=? ORDER BY created DESC LIMIT 10", (conversation["id"],))
                intake = Intake.model_validate(await complete_json_validated(self.context.llm, INTAKE_SYSTEM,
                    json.dumps({"selected_order": order.model_dump(mode="json"), "messages": list(reversed(history))}, ensure_ascii=False),
                    self.context.settings.llm_temperature, validator=Intake.model_validate,
                    schema=Intake.model_json_schema(), function_name="submit_intake"))
                if intake.kind in {"question", "greeting"}:
                    previous = await self.repo.one("SELECT run_id FROM chat_requests WHERE conversation_id=? AND run_id IS NOT NULL ORDER BY created DESC LIMIT 1", (conversation["id"],))
                    previous_run = await self.repo.run(previous["run_id"]) if previous else None
                    await self.reply(row, customer_result(previous_run["final"]) if previous_run and previous_run["final"] else "请补充具体的售后问题及期望处理方式，方便继续核对。")
                    return
                if intake.requested_action is None:
                    await self.reply(row, intake.clarification or "您希望退款、退货、换货，还是申请补偿？")
                    return
                if row["ticket_id"]:
                    ticket = await self.context.catalog.get(principal, row["ticket_id"])
                else:
                    ticket = await self.context.catalog.create(principal, order_id, intake.description, intake.user_claim,
                        intake.requested_action, intake.intent, source="chat", request_id=row["id"])
                if ticket is None:
                    await self.reply(row, "关联工单已删除，请重新提交本次诉求。", status="error")
                    return
                sid = row["session_id"] or await self.repo.create_session(principal, ticket.ticket_id)
                await self.status(row, "processing", ticket_id=ticket.ticket_id, session_id=sid, order_id=order_id)
                async with self.repo.lock:
                    await self.repo.conn.execute("UPDATE conversations SET title=?,updated=? WHERE id=?", (intake.title, time.time(), conversation["id"]))
                run = await self.runs.start(await self.repo.session(sid), principal, ticket.ticket_id, "chat-" + row["id"])
                if run["status"] == "paused":
                    run["initial"]["operator_level"] = principal["level"]
                    self.runs.launch(run, resume=True)
                await self.status(row, "processing", ticket_id=ticket.ticket_id, session_id=sid, run_id=run["id"])
            cursor = 0
            while True:
                for event in await self.repo.events(run["id"], cursor):
                    cursor = event["seq"]
                    data = event["data"]
                    if event["type"] == "update" and data.get("stage") not in {"supervisor", "final"}:
                        async with self.repo.lock:
                            await self.event_locked(conversation["id"], "progress", {"stage": data["stage"], "run_id": run["id"], "created": event["created"]})
                current = await self.repo.run(run["id"])
                if current["status"] in TERMINAL:
                    await self.reply(row, customer_result(current["final"]), ticket_id=current["ticket_id"],
                        run_id=run["id"], session_id=current["session_id"], result_status=current["status"])
                    return
                if current["status"] == "paused":
                    await self.status(row, "interrupted", run_id=run["id"])
                    return
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            current = await self.repo.one("SELECT * FROM chat_requests WHERE id=?", (row["id"],))
            if row["id"] in self.cancelling:
                if current["run_id"]:
                    await self.runs.cancel(await self.repo.run(current["run_id"]))
                await self.reply(row, "本次处理已停止，没有执行退款或赔付。", status="cancelled")
            else:
                await self.status(row, "interrupted")
        except Exception as exc:
            self.context.tracer.event("chat_error", request_id=row["id"], error=type(exc).__name__)
            await self.reply(row, "暂时无法完成本次诉求解析，请核对信息后重新提交。没有执行退款或赔付。", status="error")
        finally:
            self.cancelling.discard(row["id"])

    async def events(self, principal, cid, after=0):
        await self.own(principal, cid)
        rows = await self.repo.all("SELECT * FROM chat_events WHERE conversation_id=? AND seq>? ORDER BY seq LIMIT 1000", (cid, after))
        return [{**row, "data": json.loads(row["data"])} for row in rows]

    async def cancel(self, principal, cid):
        await self.own(principal, cid)
        row = await self.repo.one("SELECT * FROM chat_requests WHERE conversation_id=? AND status IN ('pending','processing','interrupted') ORDER BY created DESC LIMIT 1", (cid,))
        if not row:
            return
        task = self.tasks.get(row["id"])
        if task:
            self.cancelling.add(row["id"])
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        current = await self.repo.one("SELECT * FROM chat_requests WHERE id=?", (row["id"],))
        if current["status"] not in {"completed", "cancelled", "error"}:
            if current["run_id"]:
                await self.runs.cancel(await self.repo.run(current["run_id"]))
            await self.reply(row, "本次处理已停止，没有执行退款或赔付。", status="cancelled")

    async def delete(self, principal, cid):
        await self.own(principal, cid)
        active = await self.repo.one("SELECT id FROM chat_requests WHERE conversation_id=? AND status IN ('pending','processing','interrupted')", (cid,))
        if active:
            raise ValueError("会话仍在处理，请先停止处理")
        async with self.repo.lock:
            await self.repo.conn.execute("UPDATE conversations SET deleted=1 WHERE id=?", (cid,))

    async def recover(self):
        rows = await self.repo.all("SELECT * FROM chat_requests WHERE status IN ('pending','processing')")
        for row in rows:
            await self.status(row, "interrupted", ticket_id=row["ticket_id"], session_id=row["session_id"], run_id=row["run_id"])

    async def retry(self, principal, cid, request_id):
        await self.own(principal, cid)
        row = await self.repo.one("SELECT * FROM chat_requests WHERE id=? AND conversation_id=?", (request_id, cid))
        if not row or row["status"] != "interrupted":
            raise ValueError("只有中断的消息可以继续处理")
        from app.api.schemas import ChatMessageRequest
        return await self.submit(principal, cid, ChatMessageRequest(**json.loads(row["payload"]), idempotency_key=row["request_key"]))

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
