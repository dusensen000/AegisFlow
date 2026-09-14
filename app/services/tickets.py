"""Actor-scoped ticket management without mutating the shared demo catalog."""

import time
import uuid
from datetime import datetime, timezone
from app.models import Ticket
from app.data.seed import get_ticket, get_tickets, get_orders, get_order
from app.services.repository import encode


class TicketCatalog:
    def __init__(self, repository):
        self.repo = repository

    async def allowed_orders(self, principal):
        assigned = principal.get("allowed_ticket_ids")
        if assigned is None:
            return {order.order_id for order in get_orders()}
        orders = {ticket.order_id for ticket in get_tickets() if ticket.ticket_id in assigned}
        for tid in assigned:
            row = await self.repo.one("SELECT data FROM managed_tickets WHERE id=? AND tenant=?", (tid, principal["tenant"]))
            if row:
                orders.add(Ticket.model_validate_json(row["data"]).order_id)
        return orders

    async def record(self, principal, tid, *, deleted=False, team=False):
        row = await self.repo.one("SELECT * FROM managed_tickets WHERE id=? AND tenant=?", (tid, principal["tenant"]))
        if row:
            if row["owner"] != principal["actor_id"] and not (team and principal["level"] >= 3):
                return None
            ticket = Ticket.model_validate_json(row["data"])
            if ticket.order_id not in await self.allowed_orders(principal):
                return None
            if row["deleted"] and not deleted:
                return None
            return {**row, "ticket": ticket}
        ticket = get_ticket(tid)
        if not ticket or (principal.get("allowed_ticket_ids") is not None and tid not in principal["allowed_ticket_ids"]):
            return None
        overlay = await self.repo.one("SELECT * FROM ticket_overrides WHERE owner=? AND tenant=? AND id=?",
            (principal["actor_id"], principal["tenant"], tid))
        if overlay and overlay["deleted"] and not deleted:
            return None
        if overlay and overlay["data"]:
            ticket = Ticket.model_validate_json(overlay["data"])
        return {"id": tid, "owner": principal["actor_id"], "tenant": principal["tenant"],
                "source": "seed", "deleted": bool(overlay and overlay["deleted"]),
                "created": ticket.opened_at.timestamp(), "ticket": ticket}

    async def get(self, principal, tid):
        record = await self.record(principal, tid)
        return record["ticket"] if record else None

    async def list(self, principal, *, deleted=False):
        ids = [t.ticket_id for t in get_tickets()]
        rows = await self.repo.all("SELECT id FROM managed_tickets WHERE owner=? AND tenant=? ORDER BY created DESC LIMIT 200",
            (principal["actor_id"], principal["tenant"]))
        ids = [row["id"] for row in rows] + ids
        output = []
        for tid in ids:
            row = await self.record(principal, tid, deleted=True)
            if row and bool(row["deleted"]) == deleted:
                used = await self.repo.one("SELECT r.id FROM runs r JOIN sessions s ON r.session_id=s.id WHERE r.ticket_id=? AND s.owner=? AND s.tenant=? LIMIT 1",
                    (tid, row["owner"], row["tenant"]))
                output.append({**row["ticket"].model_dump(mode="json"), "source": row["source"],
                               "deleted": bool(row["deleted"]), "editable": not bool(used)})
        return output

    async def create(self, principal, order_id, description, user_claim, requested_action, intent=None, source="manual", request_id=None):
        if not description.strip():
            raise ValueError("售后问题不能为空")
        if order_id not in await self.allowed_orders(principal):
            raise PermissionError("无权使用该订单创建工单")
        order = get_order(order_id)
        if not order:
            raise ValueError("订单不存在")
        if requested_action == "退款" and order.status.value == "待发货":
            requested_action = "仅退款"
        tid = "T" + datetime.now(timezone.utc).strftime("%Y%m%d") + uuid.uuid4().hex[:12].upper()
        ticket = Ticket(ticket_id=tid, order_id=order_id, user_id=order.user_id,
            intent=intent or (requested_action if requested_action != "人工复核" else "其他"),
            description=description.strip(), user_claim=user_claim.strip() or description.strip(),
            requested_action=requested_action, verified_facts=[])
        async with self.repo.lock:
            await self.repo.conn.execute("BEGIN IMMEDIATE")
            try:
                await self.repo.conn.execute("INSERT INTO managed_tickets VALUES(?,?,?,?,?,?,?,?)",
                    (tid, principal["actor_id"], principal["tenant"], encode(ticket.model_dump(mode="json")), source, 0, time.time(), time.time()))
                if request_id:
                    await self.repo.conn.execute("UPDATE chat_requests SET ticket_id=? WHERE id=?", (tid, request_id))
                await self.repo.conn.commit()
            except BaseException:
                await self.repo.conn.rollback()
                raise
        return ticket

    async def change(self, principal, tid, *, fields=None, delete=None):
        async with self.repo.lock:
            await self.repo.conn.execute("BEGIN IMMEDIATE")
            try:
                row = await self.record(principal, tid, deleted=True)
                if not row:
                    raise LookupError("工单不存在")
                ticket = row["ticket"]
                if fields is not None:
                    used = await self.repo.one("SELECT r.id FROM runs r JOIN sessions s ON r.session_id=s.id WHERE r.ticket_id=? AND s.owner=? AND s.tenant=? LIMIT 1",
                        (tid, principal["actor_id"], principal["tenant"]))
                    if used or row["deleted"]:
                        raise ValueError("已处理或已删除的工单不能修改，补充材料请通过会话提交")
                    if fields.get("requested_action") == "退款" and get_order(ticket.order_id).status.value == "待发货":
                        fields["requested_action"] = "仅退款"
                    ticket = ticket.model_copy(update=fields)
                if delete:
                    active = await self.repo.one("SELECT r.id FROM runs r JOIN sessions s ON r.session_id=s.id WHERE r.ticket_id=? AND s.owner=? AND s.tenant=? AND r.status IN ('queued','running','paused') LIMIT 1",
                        (tid, principal["actor_id"], principal["tenant"]))
                    claimed = await self.repo.one("SELECT m.run_id FROM manual_reviews m JOIN runs r ON m.run_id=r.id JOIN sessions s ON r.session_id=s.id WHERE r.ticket_id=? AND s.owner=? AND s.tenant=? AND m.status='claimed' LIMIT 1",
                        (tid, principal["actor_id"], principal["tenant"]))
                    if active or claimed:
                        raise ValueError("工单仍在执行或已被人工领取，请先停止任务或完成复核")
                    await self.repo.conn.execute("UPDATE manual_reviews SET status='withdrawn' WHERE status='pending' AND run_id IN (SELECT r.id FROM runs r JOIN sessions s ON r.session_id=s.id WHERE r.ticket_id=? AND s.owner=? AND s.tenant=?)",
                        (tid, principal["actor_id"], principal["tenant"]))
                deleted = int(delete) if delete is not None else int(row["deleted"])
                payload = encode(ticket.model_dump(mode="json"))
                if row["source"] == "seed":
                    await self.repo.conn.execute("INSERT OR REPLACE INTO ticket_overrides VALUES(?,?,?,?,?)",
                        (principal["actor_id"], principal["tenant"], tid, payload, deleted))
                else:
                    await self.repo.conn.execute("UPDATE managed_tickets SET data=?,deleted=?,updated=? WHERE id=? AND owner=? AND tenant=?",
                        (payload, deleted, time.time(), tid, principal["actor_id"], principal["tenant"]))
                await self.repo.conn.commit()
            except BaseException:
                await self.repo.conn.rollback()
                raise
        return ticket
