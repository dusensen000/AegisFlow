"""Bound prompt evidence independently of the full audit archive."""

import json


def context_cost(text: str) -> int:
    # A conservative UTF-8 byte bound also handles long unbroken identifiers and payloads.
    return len(text.encode("utf-8"))


def bounded_evidence(results: list[dict], budget: int) -> list[dict]:
    output = []
    used = 2
    priority = {"query_order": 0, "check_after_sale_eligibility": 1, "calculate_compensation": 2, "query_logistics": 3}
    for result in sorted(results, key=lambda r: priority.get(r["tool_name"], 4)):
        item = dict(result)
        # Requested parameters are hypotheses, not observed business facts.
        item.pop("arguments", None)
        data = dict(item.get("data", {}))
        if "events" in data:
            data["events"] = data["events"][-4:]
        if "orders" in data:
            data.pop("orders")
        item["data"] = data
        cost = context_cost(json.dumps(item, ensure_ascii=False)) + (2 if output else 0)
        if used + cost <= budget:
            output.append(item)
            used += cost
        else:
            brief = {"call_id": item.get("call_id"), "tool_name": item["tool_name"],
                     "status": item["status"], "omitted": True}
            cost = context_cost(json.dumps(brief)) + (2 if output else 0)
            if used + cost <= budget:
                output.append(brief)
                used += cost
    return output
