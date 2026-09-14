import json
import httpx
import pytest
from openai import AsyncOpenAI
from app.llm.client import OpenAICompatClient, complete_json_validated
from app.tracing import Tracer, LangfuseTracer


@pytest.mark.asyncio
async def test_real_sdk_uses_function_calling_zero_temperature_and_usage(tmp_path):
    requests = []
    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0, "model": "offline",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "submit_plan", "arguments": '{"steps": []}'}}]}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})
    client = OpenAICompatClient("offline", "https://offline.invalid", "offline", 0.2)
    await client.aclose()
    client._client = AsyncOpenAI(api_key="offline", base_url="https://offline.invalid", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    client.tracer = Tracer(log_path=str(tmp_path / "trace.jsonl"))
    result = await complete_json_validated(client, "system", "user", 0,
        schema={"type": "object", "properties": {"steps": {"type": "array"}}}, function_name="submit_plan")
    assert result == {"steps": []}
    assert requests[0]["temperature"] == 0
    assert requests[0]["tool_choice"]["function"]["name"] == "submit_plan"
    assert requests[0]["tools"][0]["function"]["parameters"]["type"] == "object"
    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text().splitlines()]
    assert sum(e.get("name") == "llm_usage" for e in events) == 1
    assert any(e.get("duration_ms") is not None and e.get("name") == "llm" for e in events)
    assert not client.tracer._spans
    await client.aclose()


def test_langfuse_v4_correlates_generations_and_usage():
    class Observation:
        trace_id = "0" * 32
        id = "1" * 16
        def update(self, **kwargs):
            self.updated = kwargs
        def end(self):
            self.ended = True
    class Client:
        def __init__(self):
            self.calls = []
        def start_observation(self, **kwargs):
            observation = Observation()
            self.calls.append((kwargs, observation))
            return observation
    tracer = LangfuseTracer(enabled=False)
    tracer.client = Client()
    run = tracer.start_span("run", run_id="test")
    generation = tracer.start_span("llm", model="offline")
    tracer.event("llm_usage", model="offline", usage={"prompt_tokens": 100, "completion_tokens": 20})
    assert tracer.client.calls[1][0]["as_type"] == "generation"
    assert tracer.client.calls[1][0]["trace_context"]["parent_span_id"] == "1" * 16
    assert tracer.client.calls[1][1].updated["usage_details"] == {"input": 100, "output": 20}
    tracer.end_span(generation)
    tracer.end_span(run)
    assert not tracer._spans and not tracer.observations
