"""Per-run immutable ticket snapshot; safe across concurrent graph executions."""

from contextvars import ContextVar

CURRENT_TICKET = ContextVar("current_business_ticket", default=None)
