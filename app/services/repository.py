"""Durable sessions, runs, event replay, evidence and manual-review decisions."""

import asyncio
import hashlib
import json
import time
import uuid
from pathlib import Path

import aiosqlite


def encode(value):
    return json.dumps(value, ensure_ascii=False)


class Repository:
    def __init__(self, path: str):
        self.path = path
        self.conn = None
        self.lock = asyncio.Lock()

    async def open(self):
        self.lock = asyncio.Lock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS identities(token_hash TEXT PRIMARY KEY, data TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, owner TEXT, tenant TEXT, ticket_id TEXT, state TEXT, supplements TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, session_id TEXT, ticket_id TEXT, status TEXT, initial TEXT, final TEXT, request_key TEXT, created REAL);
            CREATE UNIQUE INDEX IF NOT EXISTS active_run ON runs(session_id) WHERE status IN ('queued','running','paused');
            CREATE UNIQUE INDEX IF NOT EXISTS request_run ON runs(session_id, request_key);
            CREATE TABLE IF NOT EXISTS events(run_id TEXT, seq INTEGER, type TEXT, data TEXT, created REAL, PRIMARY KEY(run_id,seq));
            CREATE TABLE IF NOT EXISTS evidence(call_id TEXT PRIMARY KEY, run_id TEXT, data TEXT);
            CREATE TABLE IF NOT EXISTS manual_reviews(run_id TEXT PRIMARY KEY, tenant TEXT, status TEXT, assignee TEXT, decision TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS feedback(run_id TEXT PRIMARY KEY, actor TEXT, data TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS managed_tickets(id TEXT PRIMARY KEY, owner TEXT, tenant TEXT, data TEXT, source TEXT, deleted INTEGER, created REAL, updated REAL);
            CREATE TABLE IF NOT EXISTS ticket_overrides(owner TEXT, tenant TEXT, id TEXT, data TEXT, deleted INTEGER, PRIMARY KEY(owner,tenant,id));
            CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, owner TEXT, tenant TEXT, title TEXT, order_id TEXT, deleted INTEGER DEFAULT 0, created REAL, updated REAL);
            CREATE TABLE IF NOT EXISTS chat_requests(id TEXT PRIMARY KEY, conversation_id TEXT, request_key TEXT, payload TEXT, status TEXT, ticket_id TEXT, session_id TEXT, run_id TEXT, created REAL, UNIQUE(conversation_id,request_key));
            CREATE UNIQUE INDEX IF NOT EXISTS active_chat_request ON chat_requests(conversation_id) WHERE status IN ('pending','processing');
            CREATE TABLE IF NOT EXISTS chat_messages(id TEXT PRIMARY KEY, conversation_id TEXT, request_id TEXT, role TEXT, content TEXT, data TEXT, created REAL, UNIQUE(request_id,role));
            CREATE TABLE IF NOT EXISTS chat_events(conversation_id TEXT, seq INTEGER, type TEXT, data TEXT, created REAL, PRIMARY KEY(conversation_id,seq));
        """)
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def one(self, sql, params=()):
        async with self.conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def all(self, sql, params=()):
        async with self.conn.execute(sql, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def identity(self, token):
        row = await self.one("SELECT data FROM identities WHERE token_hash=? AND expires>?", (hashlib.sha256(token.encode()).hexdigest(), time.time()))
        return json.loads(row["data"]) if row else None

    async def save_identity(self, token, principal):
        async with self.lock:
            await self.conn.execute("INSERT OR REPLACE INTO identities VALUES(?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), encode(principal), time.time() + 86400 * 30))
            await self.conn.commit()

    async def create_session(self, principal, ticket_id=None):
        sid = uuid.uuid4().hex
        async with self.lock:
            await self.conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?)",
                (sid, principal["actor_id"], principal["tenant"], ticket_id, "{}", "[]", time.time()))
            await self.conn.commit()
        return sid

    async def session(self, sid):
        row = await self.one("SELECT * FROM sessions WHERE id=?", (sid,))
        if row:
            row["state"] = json.loads(row["state"])
            row["supplements"] = json.loads(row["supplements"])
        return row

    async def list_sessions(self, principal):
        return await self.all("SELECT id,ticket_id,created FROM sessions WHERE owner=? AND tenant=? ORDER BY created DESC LIMIT 100", (principal["actor_id"], principal["tenant"]))

    async def get_state(self, sid):
        session = await self.session(sid)
        return session["state"] if session else None

    async def set_state(self, sid, state):
        async with self.lock:
            await self.conn.execute("UPDATE sessions SET state=? WHERE id=?", (encode(state), sid))
            await self.conn.commit()

    async def supplement(self, sid, text):
        async with self.lock:
            row = await self.session(sid)
            active = await self.one("SELECT id FROM runs WHERE session_id=? AND status IN ('queued','running','paused')", (sid,))
            if active:
                raise ValueError("请先取消或完成当前任务，再补充材料")
            values = (row["supplements"] + [text])[-5:]
            await self.conn.execute("UPDATE sessions SET supplements=? WHERE id=?", (encode(values), sid))
            await self.conn.commit()

    async def create_run(self, sid, ticket_id, initial, request_key):
        async with self.lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                existing = await self.one("SELECT * FROM runs WHERE session_id=? AND request_key=?", (sid, request_key))
                if existing:
                    if existing["ticket_id"] != ticket_id:
                        raise ValueError("幂等键已用于不同工单")
                    await self.conn.rollback()
                    return existing, False
                session = await self.session(sid)
                if session["ticket_id"] not in (None, ticket_id):
                    raise ValueError("会话已绑定其他工单")
                active = await self.one("SELECT id FROM runs WHERE session_id=? AND status IN ('queued','running','paused')", (sid,))
                if active:
                    raise ValueError("会话已有活动任务")
                await self.conn.execute("UPDATE sessions SET ticket_id=? WHERE id=?", (ticket_id, sid))
                await self.conn.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)",
                    (initial["run_id"], sid, ticket_id, "queued", encode(initial), None, request_key, time.time(),))
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise
        return await self.run(initial["run_id"]), True

    async def run(self, rid):
        row = await self.one("SELECT * FROM runs WHERE id=?", (rid,))
        if row:
            row["initial"] = json.loads(row["initial"])
            row["final"] = json.loads(row["final"]) if row["final"] else None
        return row

    async def session_runs(self, sid):
        return await self.all("SELECT id,status,ticket_id,created FROM runs WHERE session_id=? ORDER BY created DESC LIMIT 100", (sid,))

    async def update_run(self, rid, status, final=None):
        async with self.lock:
            await self.conn.execute("UPDATE runs SET status=?, final=COALESCE(?,final) WHERE id=?", (status, encode(final) if final else None, rid))
            await self.conn.commit()

    async def recover(self):
        async with self.lock:
            await self.conn.execute("UPDATE runs SET status='paused' WHERE status IN ('queued','running')")
            await self.conn.commit()

    async def append_event(self, rid, event_type, data):
        async with self.lock:
            row = await self.one("SELECT COALESCE(MAX(seq),0)+1 AS seq FROM events WHERE run_id=?", (rid,))
            seq = row["seq"]
            await self.conn.execute("INSERT INTO events VALUES(?,?,?,?,?)", (rid, seq, event_type, encode(data), time.time()))
            for result in data.get("delta", {}).get("tool_results", []):
                if result.get("call_id"):
                    await self.conn.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,?)", (result["call_id"], rid, encode(result)))
            await self.conn.commit()
        return seq

    async def events(self, rid, after=0):
        rows = await self.all("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 1000", (rid, after))
        return [{**r, "data": json.loads(r["data"])} for r in rows]

    async def enqueue_manual(self, rid, tenant):
        async with self.lock:
            await self.conn.execute("INSERT OR IGNORE INTO manual_reviews VALUES(?,?,?,?,?,?)", (rid, tenant, "pending", None, None, time.time()))
            await self.conn.commit()

    async def complete_run(self, row, final):
        async with self.lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                await self.conn.execute("UPDATE sessions SET state=? WHERE id=?", (encode(final), row["session_id"]))
                if final["status"] == "manual_review":
                    session = await self.session(row["session_id"])
                    await self.conn.execute("INSERT OR IGNORE INTO manual_reviews VALUES(?,?,?,?,?,?)", (row["id"], session["tenant"], "pending", None, None, time.time()))
                sequence = await self.one("SELECT COALESCE(MAX(seq),0)+1 AS seq FROM events WHERE run_id=?", (row["id"],))
                done = {"run_id": row["id"], "thread_id": row["session_id"], "status": final["status"],
                    "step_count": final.get("step_count", 0), "tool_call_count": final.get("tool_call_count", 0), "errors": final.get("errors", [])}
                await self.conn.execute("INSERT INTO events VALUES(?,?,?,?,?)", (row["id"], sequence["seq"], "done", encode(done), time.time()))
                await self.conn.execute("UPDATE runs SET status=?,final=? WHERE id=?", (final["status"], encode(final), row["id"]))
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise

    async def manual_list(self, principal):
        if principal["level"] >= 3:
            return await self.all("SELECT m.*,r.session_id,r.ticket_id,r.final FROM manual_reviews m JOIN runs r ON m.run_id=r.id WHERE m.tenant=? ORDER BY m.created DESC LIMIT 100", (principal["tenant"],))
        return await self.all("SELECT m.*,r.session_id,r.ticket_id,r.final FROM manual_reviews m JOIN runs r ON m.run_id=r.id JOIN sessions s ON r.session_id=s.id WHERE s.owner=? AND m.tenant=? ORDER BY m.created DESC LIMIT 100", (principal["actor_id"], principal["tenant"]))

    async def manual_decision(self, rid, principal, decision=None):
        async with self.lock:
            row = await self.one("SELECT * FROM manual_reviews WHERE run_id=? AND tenant=?", (rid, principal["tenant"]))
            if not row:
                raise ValueError("人工任务不存在")
            if decision is None:
                if row["status"] != "pending":
                    raise ValueError("任务已被领取或处理")
                await self.conn.execute("UPDATE manual_reviews SET status='claimed',assignee=? WHERE run_id=?", (principal["actor_id"], rid))
            else:
                if row["status"] != "claimed" or row["assignee"] != principal["actor_id"]:
                    raise ValueError("需由领取任务的客服提交复核结果")
                await self.conn.execute("UPDATE manual_reviews SET status='resolved',decision=? WHERE run_id=?", (encode(decision), rid))
            await self.conn.commit()

    async def save_feedback(self, rid, actor, data):
        async with self.lock:
            await self.conn.execute("INSERT OR REPLACE INTO feedback VALUES(?,?,?,?)", (rid, actor, encode(data), time.time()))
            await self.conn.commit()


class DurableSessionStore:
    def __init__(self, repository, cache=None):
        self.repository, self.cache = repository, cache

    async def get(self, sid):
        # The database remains authoritative when Redis expires or is unavailable.
        return await self.repository.get_state(sid)

    async def set(self, sid, state):
        await self.repository.set_state(sid, state)
        if self.cache:
            try:
                await self.cache.set(sid, state)
            except Exception:
                pass
