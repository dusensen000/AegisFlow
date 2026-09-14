"""Evidence and rule-reference diagnostics; not semantic quality scores."""

from __future__ import annotations

import re


def estimate_tokens(text: str) -> int:
    """粗略估算 token：中文字符按 1，英文按词。"""
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    english = len(re.findall(r"[a-zA-Z0-9]+", text))
    return chinese + english


def completeness_score(
    proposal: dict | None,
    required_tools: list[str],
) -> float:
    """方案证据完整率：required_tools 中出现在证据里的比例。"""
    if not proposal:
        return 0.0
    evidence_tools: set[str] = set()
    for evidence in proposal.get("evidence", []):
        if isinstance(evidence, dict) and evidence.get("status") == "ok":
            evidence_tools.add(evidence.get("tool_name", ""))
    if not required_tools:
        return 1.0
    matched = sum(1 for tool in required_tools if tool in evidence_tools)
    return matched / len(required_tools)


def rule_compliance_score(
    proposal: dict | None,
    known_rule_ids: set[str],
) -> float:
    """规则符合率：所有方案项中引用有效规则的比例。"""
    if not proposal:
        return 0.0
    total_refs = 0
    valid_refs = 0
    for item in proposal.get("items", []):
        refs = item.get("rule_refs", [])
        total_refs += len(refs)
        valid_refs += sum(1 for ref in refs if ref in known_rule_ids)
    return valid_refs / total_refs if total_refs else 0.0


def hallucination_rate(
    proposal: dict | None,
    known_rule_ids: set[str],
) -> float:
    """幻觉率：引用不存在规则的比例，越低越好。"""
    if not proposal:
        return 1.0
    total_refs = 0
    invalid_refs = 0
    for item in proposal.get("items", []):
        for ref in item.get("rule_refs", []):
            total_refs += 1
            if ref not in known_rule_ids:
                invalid_refs += 1
    return invalid_refs / total_refs if total_refs else 0.0


def build_metrics(
    proposal: dict | None,
    required_tools: list[str],
    known_rule_ids: set[str],
    review: dict | None,
    step_count: int,
    replan_count: int,
) -> dict:
    return {
        "evidence_coverage": round(completeness_score(proposal, required_tools), 4),
        "rule_reference_validity": round(
            rule_compliance_score(proposal, known_rule_ids),
            4,
        ),
        "unknown_rule_reference_rate": round(
            hallucination_rate(proposal, known_rule_ids),
            4,
        ),
        "validation_passed": bool(review.get("passed")) if review else False,
        "step_count": step_count,
        "replan_count": replan_count,
        "estimated_tokens": estimate_tokens(
            str(proposal or {}) + str(review or {})
        ),
    }


def aggregate_metrics(case_metrics: list[dict]) -> dict:
    if not case_metrics:
        return {}
    keys = [
        "evidence_coverage",
        "rule_reference_validity",
        "unknown_rule_reference_rate",
        "validation_passed",
        "step_count",
        "replan_count",
        "estimated_tokens",
    ]
    summary = {}
    for key in keys:
        values = [item[key] for item in case_metrics if key in item]
        if key == "validation_passed":
            summary[key] = round(sum(values) / len(values), 4)
        else:
            summary[key] = round(sum(values) / len(values), 4) if values else 0
    return summary
