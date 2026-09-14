import importlib.util
import json
import pytest
import httpx
from openai import AsyncOpenAI


@pytest.mark.asyncio
async def test_ragas_factory_runs_actual_async_statement_and_nli_pipeline():
    if importlib.util.find_spec("ragas") is None:
        pytest.skip("Optional evaluation dependencies are not installed")
    from eval.evaluate import RagasEvaluator
    calls = []
    def response(request):
        body = json.loads(request.content)
        assert body["response_format"]["type"] == "json_object"
        name = "StatementGeneratorOutput" if not calls else "NLIStatementOutput"
        calls.append(name)
        if name == "StatementGeneratorOutput":
            payload = {"statements": ["Compensation is 19.95"]}
        else:
            assert name == "NLIStatementOutput"
            payload = {"statements": [{"statement": "Compensation is 19.95", "reason": "supported", "verdict": 1}]}
        return httpx.Response(200, json={"id":"offline", "object":"chat.completion", "created":0, "model":"offline",
            "choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":json.dumps(payload)}}],
            "usage":{"prompt_tokens":10,"completion_tokens":10,"total_tokens":20}})
    client = AsyncOpenAI(api_key="offline", base_url="https://offline.invalid", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(response)))
    evaluator = RagasEvaluator(client, "offline")
    state = {"ticket":{"description":"logistics stopped"},
        "tool_results":[{"status":"ok","data":{"amount":19.95}}], "retrieved_rules":[],
        "proposal":{"summary":"Compensation is 19.95","items":[{"action":"补偿","reason":"Delay", "amount":19.95,"rule_refs":["R004"]}]}}
    result = await evaluator.evaluate(state)
    assert result == {"enabled":True,"faithfulness":1.0}
    assert calls == ["StatementGeneratorOutput", "NLIStatementOutput"]
    await client.close()
