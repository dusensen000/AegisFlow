"""Decouple graph execution from SSE connections and persist every public event."""

import asyncio
from app.graph import build_initial_state, graph_config, stream_agent
from app.data.seed import get_ticket
from app.models import Ticket

TERMINAL = {"completed", "failed", "manual_review", "cancelled"}


class RunManager:
    def __init__(self, context, repository):
        self.context, self.repository = context, repository
        self.tasks = {}
        self.cancelling = set()

    async def start(self, session, principal, ticket_id, request_key):
        ticket = await self.context.catalog.get(principal, ticket_id)
        if not ticket:
            raise ValueError("工单不存在或已删除")
        previous = session["state"]
        memory = previous.get("memory") if (previous.get("ticket") or {}).get("ticket_id") == ticket_id else None
        initial = build_initial_state(self.context, ticket, session["id"], actor_id=principal["actor_id"],
            operator_level=principal["level"], memory=memory, supplements=session["supplements"])
        row, created = await self.repository.create_run(session["id"], ticket_id, initial, request_key)
        row = await self.repository.run(row["id"])
        if created:
            self.launch(row)
        return row

    def launch(self, row, resume=False):
        rid = row["id"]
        if rid in self.tasks:
            raise ValueError("任务已在执行")
        task = asyncio.create_task(self.execute(row, resume), name="run-" + rid)
        self.tasks[rid] = task
        task.add_done_callback(lambda completed: self.tasks.pop(rid, None))

    async def finish(self, row, final):
        await self.repository.complete_run(row, final)

    async def execute(self, row, resume=False):
        span = self.context.tracer.start_span("run", run_id=row["id"], thread_id=row["session_id"])
        try:
            await self.repository.update_run(row["id"], "running")
            await self.repository.append_event(row["id"], "started", {"thread_id": row["session_id"], "run_id": row["id"], "resumed": resume})
            if resume:
                snapshot = await self.context.graph.aget_state(graph_config(row["initial"]))
                if snapshot.values and snapshot.next:
                    await self.context.graph.aupdate_state(graph_config(row["initial"]),
                        {"operator_level": row["initial"]["operator_level"]})
                if snapshot.values and not snapshot.next:
                    final = dict(snapshot.values)
                    await self.repository.append_event(row["id"], "update", {"stage": "final", "delta": final})
                    await self.finish(row, final)
                    return
                resume = bool(snapshot.values)
            async for event in stream_agent(self.context, Ticket.model_validate(row["initial"]["ticket"]), row["session_id"],
                                            initial=row["initial"], resume=resume):
                await self.repository.append_event(row["id"], "update", event)
                if event["stage"] == "final":
                    await self.finish(row, event["delta"])
        except asyncio.CancelledError:
            current = await self.repository.run(row["id"])
            if current["status"] in TERMINAL:
                return
            status = "cancelled" if row["id"] in self.cancelling else "paused"
            state = await self.context.store.get(row["session_id"]) or row["initial"]
            if status == "cancelled":
                state = {**state, "status": status}
                await self.repository.append_event(row["id"], "update", {"stage": "final", "delta": state})
                await self.finish(row, state)
            else:
                await self.repository.update_run(row["id"], status)
                await self.repository.append_event(row["id"], "paused", {"status": status, "run_id": row["id"]})
        except Exception as exc:
            state = await self.context.store.get(row["session_id"]) or row["initial"]
            state = {**state, "status": "failed", "errors": state.get("errors", []) + [type(exc).__name__]}
            await self.repository.append_event(row["id"], "update", {"stage": "final", "delta": state})
            await self.finish(row, state)
        finally:
            current = await self.repository.run(row["id"])
            self.context.tracer.end_span(span, status=current["status"] if current else "failed")
            self.cancelling.discard(row["id"])
            if hasattr(self.context.bus, "delete"):
                await self.context.bus.delete(row["id"])

    async def cancel(self, row):
        current = await self.repository.run(row["id"])
        if current["status"] in TERMINAL:
            return
        task = self.tasks.get(row["id"])
        if task:
            self.cancelling.add(row["id"])
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            current = await self.repository.run(row["id"])
            if current["status"] not in TERMINAL:
                state = await self.context.store.get(row["session_id"]) or row["initial"]
                await self.finish(row, {**state, "status": "cancelled"})
        elif row["status"] == "paused":
            state = await self.context.store.get(row["session_id"]) or row["initial"]
            await self.repository.append_event(row["id"], "update", {"stage": "final", "delta": {**state, "status": "cancelled"}})
            await self.finish(row, {**state, "status": "cancelled"})

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
