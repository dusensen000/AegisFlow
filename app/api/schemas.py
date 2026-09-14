"""API 请求与响应结构。"""

from __future__ import annotations

from typing import Optional, Literal
import uuid

from pydantic import BaseModel, Field, ConfigDict


class CreateSessionResponse(BaseModel):
    thread_id: str


class RunAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket_id: str
    thread_id: Optional[str] = None
    idempotency_key: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1, max_length=100)


class RunAgentResponse(BaseModel):
    thread_id: str
    status: str
    proposal: Optional[dict] = None
    review: Optional[dict] = None
    errors: list[str] = Field(default_factory=list)
    step_count: int = 0


class AuthRequest(BaseModel):
    api_token: str | None = None


class SessionRequest(BaseModel):
    ticket_id: str | None = None


class SupplementRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class DecisionRequest(BaseModel):
    outcome: str = Field(pattern="^(approved|rejected|needs_evidence)$")
    note: str = Field(min_length=1, max_length=2000)


class FeedbackRequest(BaseModel):
    outcome: str = Field(pattern="^(accepted|rejected)$")
    note: str = Field(default="", max_length=2000)


class TicketCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    order_id: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=2000)
    user_claim: str = Field(default="", max_length=2000)
    requested_action: Literal["退款", "退货", "换货", "补偿", "仅退款", "人工复核"]


class TicketEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    description: str | None = Field(default=None, min_length=1, max_length=2000)
    user_claim: str | None = Field(default=None, min_length=1, max_length=2000)
    requested_action: Literal["退款", "退货", "换货", "补偿", "仅退款", "人工复核"] | None = None


class ConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str | None = None


class ChatMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=2000)
    order_id: str | None = None
    idempotency_key: str = Field(default_factory=lambda: uuid.uuid4().hex, min_length=1, max_length=100)
