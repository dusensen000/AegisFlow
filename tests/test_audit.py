from datetime import datetime, timezone
import pytest
from app.models import Proposal, ProposalItem
from app.data.seed import get_ticket, get_order, get_logistics
from app.services.proposal_audit import audit_proposal


def case(context):
    ticket = get_ticket("T20260201001").model_copy(update={"opened_at": datetime(2026, 1, 10, tzinfo=timezone.utc)})
    order = get_order(ticket.order_id)
    results = [context.tools.execute(name, {"order_id": ticket.order_id}).model_dump(mode="json") for name in ("query_order", "query_logistics")]
    results.append({"call_id": "eligibility", "tool_name": "check_after_sale_eligibility", "status": "ok",
                    "data": {"order_id": ticket.order_id, "ticket_id": ticket.ticket_id},
                    "arguments": {"requested_action": ticket.requested_action}})
    item = ProposalItem(action="退款", reason="Within seven days", amount=299,
        rule_refs=["R001"], evidence_tools=["query_order", "query_logistics"])
    return ticket, order, results, item


@pytest.mark.parametrize("change", [
    {"amount": -1}, {"amount": 1}, {"amount": 999999}, {"amount": None}, {"amount": 299.001},
    {"rule_refs": []}, {"rule_refs": ["R003"]}, {"evidence_tools": ["invented"]},
    {"evidence_ids": ["invented-call"]},
])
def test_invalid_money_rules_and_evidence_cannot_pass(context, change):
    ticket, order, results, item = case(context)
    proposal = Proposal(ticket_id=ticket.ticket_id, summary="test", items=[item.model_copy(update=change)])
    assert audit_proposal(proposal, ticket, order, get_logistics(ticket.order_id), results, {"R001", "R003"})


def test_valid_rule_requires_retrieval_and_refund_actions_are_exclusive(context):
    ticket, order, results, item = case(context)
    proposal = Proposal(ticket_id=ticket.ticket_id, summary="test", items=[item])
    assert not audit_proposal(proposal, ticket, order, get_logistics(ticket.order_id), results, {"R001"})
    assert audit_proposal(proposal, ticket, order, get_logistics(ticket.order_id), results, set())
    proposal.items.append(item.model_copy())
    assert audit_proposal(proposal, ticket, order, get_logistics(ticket.order_id), results, {"R001"})
