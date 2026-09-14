"""领域模型与 Pydantic 数据结构。"""

from __future__ import annotations

from enum import Enum
from datetime import datetime, timezone
from typing import Optional, Literal, Annotated

from pydantic import BaseModel, Field, ConfigDict, field_validator


class OrderStatus(str, Enum):
    PENDING = "待发货"
    SHIPPED = "已发货"
    DELIVERED = "已签收"
    IN_AFTER_SALE = "售后中"
    CLOSED = "已关闭"


class Order(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    order_id: str
    user_id: str
    product_name: str
    amount: float = Field(ge=0)
    status: OrderStatus
    created_at: str
    payment_method: str
    after_sale_count: int = Field(default=0, ge=0)
    protection_expired: bool = False


class LogisticsEvent(BaseModel):
    time: str
    status: str
    location: str

    @field_validator("time")
    @classmethod
    def valid_time(cls, value):
        datetime.fromisoformat(value)
        return value


class LogisticsInfo(BaseModel):
    order_id: str
    carrier: str
    tracking_no: str
    current_status: str
    events: list[LogisticsEvent] = Field(default_factory=list)


class RuleCategory(str, Enum):
    REFUND = "退款"
    RETURN = "退货"
    EXCHANGE = "换货"
    COMPENSATION = "补偿"
    ONLY_REFUND = "仅退款"


class AfterSaleRule(BaseModel):
    rule_id: str
    category: str
    condition: str
    description: str
    permission_level: int = Field(default=1, ge=1, le=3)
    actions: list[str] = Field(default_factory=list)
    source: str = "sop/manual"


class TicketIntent(str, Enum):
    REFUND = "退款"
    RETURN = "退货"
    EXCHANGE = "换货"
    COMPENSATION = "补偿"
    LOGISTICS = "物流异常"
    DAMAGE = "商品破损"
    MISSING_ITEM = "少件"


class Ticket(BaseModel):
    ticket_id: str
    order_id: str
    user_id: str
    intent: str
    description: str
    user_claim: str
    requested_action: str
    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    verified_facts: list[str] = Field(default_factory=list)


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: str
    agent: str = Field(pattern="^(tool|memory|reviewer)$")
    action: str
    tool_name: Optional[str] = None
    arguments: dict = Field(default_factory=dict)
    expected_output: str = ""
    depends_on: list[str] = Field(default_factory=list)


class ToolCallResult(BaseModel):
    call_id: str = ""
    step_id: str = ""
    tool_name: str
    status: str = Field(pattern="^(ok|error|skipped)$")
    data: dict = Field(default_factory=dict)
    error: Optional[str] = None
    arguments: dict = Field(default_factory=dict)


class AgentMemory(BaseModel):
    summary: str = ""
    facts: list[str] = Field(default_factory=list)
    recent_steps: list[str] = Field(default_factory=list)
    compressed: bool = False


class ProposalItem(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    action: Literal["退款", "退货", "换货", "补偿", "仅退款", "人工复核"]
    reason: str
    rule_refs: list[Annotated[str, Field(pattern=r"^R\d{3}$")]] = Field(default_factory=list)
    amount: Optional[float] = None
    evidence_tools: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class ProposalDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    ticket_id: str
    summary: str
    items: list[ProposalItem] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0, le=1)
    follow_up: list[str] = Field(default_factory=list)


class Proposal(ProposalDraft):
    evidence: list[ToolCallResult] = Field(default_factory=list)


class ReviewResult(BaseModel):
    passed: bool
    issues: list[str] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    needs_replan: bool = False
    needs_manual_review: bool = False
    replan_count: int = 0
