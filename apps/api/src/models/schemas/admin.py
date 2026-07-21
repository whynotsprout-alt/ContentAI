from __future__ import annotations

from datetime import datetime
from typing import Literal

from models.schemas.base import InputSchemaBase, SchemaBase
from models.schemas.chat import ChatMessageResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class AdminUserUpdate(InputSchemaBase):
    role: Literal["user", "admin"]


class TemporaryPasswordResponse(SchemaBase):
    temporary_password: str
    expires_at: datetime


class AdminSessionSummary(SchemaBase):
    session_id: str
    user_id: str
    user_email: str
    agent_id: str
    title: str
    message_count: int = 0
    latest_execution_status: str | None = None
    updated_at: datetime


class AdminSessionListResponse(SchemaBase):
    items: list[AdminSessionSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class AdminSessionDetail(AdminSessionSummary):
    pass


class AdminMessageListResponse(SchemaBase):
    items: list[ChatMessageResponse] = Field(default_factory=list)
    next_cursor: str | None = None


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


class ModelConfigurationResponse(SchemaBase):
    configured: bool
    id: str | None = None
    version: int | None = None
    provider: str | None = None
    base_url: str | None = None
    model_name: str | None = None
    api_key_hint: str | None = None
    validated_at: datetime | None = None
    created_at: datetime | None = None
    created_by_user_id: str | None = None
    created_by_email: str | None = None


class ModelConfigurationProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(max_length=2048)
    api_key: SecretStr | None = Field(default=None, max_length=4096)
    model_name: str | None = Field(default=None, max_length=256)

    @field_validator("base_url", "model_name")
    @classmethod
    def strip_non_secret_strings(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("string cannot be blank or whitespace")
        return value


class ModelConfigurationUpdateRequest(ModelConfigurationProbeRequest):
    model_name: str = Field(max_length=256)
    expected_version: int = Field(ge=0)


class ModelConfigurationProbeResponse(SchemaBase):
    model_config = ConfigDict(
        from_attributes=True,
        protected_namespaces=("model_dump",),
    )

    base_url: str
    models: list[str] = Field(default_factory=list)
    models_truncated: bool
    model_validated: bool
    latency_ms: int = Field(ge=0)
