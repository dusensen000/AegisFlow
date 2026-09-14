import pytest
from eval.evaluate import run_evaluation
from eval.metrics import build_metrics


def test_claimed_and_failed_tools_do_not_count_as_evidence():
    proposal = {"items": [{"evidence_tools": ["query_order"]}],
                "evidence": [{"tool_name": "query_order", "status": "error"}]}
    assert build_metrics(proposal, ["query_order"], set(), None, 0, 0)["evidence_coverage"] == 0


@pytest.mark.asyncio
async def test_offline_evaluation_uses_real_lifecycle_and_reports_skips(context, tmp_path):
    report = await run_evaluation(limit=2, context=context, output=str(tmp_path / "report.json"))
    assert report["case_count"] == 2
    assert report["cases"][1]["status"] == "completed"
    assert not report["cases"][1]["ragas"]["enabled"]
    assert not report["cases"][1]["llm_judge"]["enabled"]
    assert report["prompt_sources"]
