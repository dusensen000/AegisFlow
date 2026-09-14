"""Correlated local traces and optional Langfuse v4 observations."""

import json
import logging
import time
import uuid
from contextvars import ContextVar
from pathlib import Path

ACTIVE_SPAN = ContextVar("aegis_span", default=None)


class Tracer:
    def __init__(self, enabled=True, log_path="logs/traces.jsonl"):
        self.enabled = enabled
        self.log_path = Path(log_path)
        self._spans = {}

    def start_span(self, name, **metadata):
        sid = uuid.uuid4().hex
        parent = ACTIVE_SPAN.get()
        inherited = self._spans.get(parent, {})
        for key in ("run_id", "thread_id"):
            if inherited.get(key):
                metadata.setdefault(key, inherited[key])
        token = ACTIVE_SPAN.set(sid)
        self._spans[sid] = {"started": time.perf_counter(), "token": token,
                            "parent_span_id": parent, "name": name, **metadata}
        self._record({"type": "span_start", "span_id": sid, "parent_span_id": parent, "name": name, **metadata})
        return sid

    def end_span(self, span_id, status="ok", **metadata):
        span = self._spans.pop(span_id, None)
        if span:
            self._record({"type": "span_end", "span_id": span_id, "status": status,
                          "parent_span_id": span["parent_span_id"], "name": span["name"],
                          "run_id": span.get("run_id"), "thread_id": span.get("thread_id"),
                          "duration_ms": round((time.perf_counter() - span["started"]) * 1000, 2), **metadata})
            ACTIVE_SPAN.reset(span["token"])

    def event(self, name, **payload):
        span = self._spans.get(ACTIVE_SPAN.get(), {})
        self._record({"type": "event", "name": name, "span_id": ACTIVE_SPAN.get(),
                      "run_id": span.get("run_id"), "thread_id": span.get("thread_id"), **payload})

    def _record(self, payload):
        if not self.enabled:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"ts": time.time(), **payload}, ensure_ascii=False) + "\n")
        except OSError:
            logging.getLogger(__name__).exception("Cannot write local trace")


class LangfuseTracer(Tracer):
    def __init__(self, enabled=True, log_path="logs/traces.jsonl"):
        super().__init__(enabled, log_path)
        self.client = None
        self.observations = {}
        if enabled:
            from langfuse import Langfuse
            self.client = Langfuse()

    def start_span(self, name, **metadata):
        parent = ACTIVE_SPAN.get()
        sid = super().start_span(name, **metadata)
        if self.client:
            try:
                parent_observation = self.observations.get(parent)
                kwargs = {"name": name, "as_type": "generation" if name == "llm" else ("agent" if name != "run" else "span"), "metadata": metadata}
                if name == "llm":
                    kwargs["model"] = metadata.get("model")
                if parent_observation:
                    kwargs["trace_context"] = {"trace_id": parent_observation.trace_id, "parent_span_id": parent_observation.id}
                self.observations[sid] = self.client.start_observation(**kwargs)
            except Exception:
                logging.getLogger(__name__).exception("Cannot create Langfuse observation")
        return sid

    def end_span(self, span_id, status="ok", **metadata):
        observation = self.observations.pop(span_id, None)
        if observation:
            try:
                observation.update(output={"status": status, **metadata})
                observation.end()
            except Exception:
                logging.getLogger(__name__).exception("Cannot finish Langfuse observation")
        super().end_span(span_id, status, **metadata)

    def event(self, name, **payload):
        super().event(name, **payload)
        parent = self.observations.get(ACTIVE_SPAN.get())
        if parent and name == "llm_usage":
            try:
                usage = payload.get("usage") or {}
                parent.update(usage_details={"input": usage.get("prompt_tokens", 0), "output": usage.get("completion_tokens", 0)})
            except Exception:
                logging.getLogger(__name__).exception("Cannot record Langfuse generation")

    def flush(self):
        if self.client:
            self.client.flush()
