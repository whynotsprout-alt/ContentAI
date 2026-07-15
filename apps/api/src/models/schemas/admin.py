from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from models.schemas.base import InputSchemaBase, SchemaBase
from pydantic import Field


class AdminUserUpdate(InputSchemaBase):
    status: Literal["active", "disabled"] | None = None
    role: Literal["user", "admin"] | None = None


class AdminSessionSummary(SchemaBase):
    session_id: str
    user_id: str
    user_email: str
    agent_id: str
    title: str
    status: str
    message_count: int = 0
    latest_execution_status: str | None = None
    updated_at: datetime


class AdminSessionListResponse(SchemaBase):
    items: list[AdminSessionSummary] = Field(default_factory=list)
    page: int
    page_size: int
    total: int


class AdminSessionDetail(AdminSessionSummary):
    messages: list[dict[str, Any]] = Field(default_factory=list)


class AdminUsageBucket(SchemaBase):
    bucket: str
    user_id: str | None = None
    model_name: str | None = None
    category: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    call_count: int = 0
    failed_call_count: int = 0
    average_latency_ms: float | None = None


class AdminUsageResponse(SchemaBase):
    items: list[AdminUsageBucket] = Field(default_factory=list)
