from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from contentai.models.model_configuration import (
    MODEL_PRICE_MAX_USD,
    MODEL_PRICE_PRECISION,
    MODEL_PRICE_SCALE,
)
from contentai.models.schemas.base import InputSchemaBase, SchemaBase
from contentai.models.schemas.chat import ChatMessageResponse


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
    input_cost_usd: float = 0
    output_cost_usd: float = 0
    total_cost_usd: float = 0
    call_count: int = 0
    completed_call_count: int = 0
    missing_usage_call_count: int = 0
    failed_call_count: int = 0
    average_latency_ms: float | None = None


class AdminUsageResponse(SchemaBase):
    items: list[AdminUsageBucket] = Field(default_factory=list)


class ModelRuntimeParameters(BaseModel):
    temperature: float | None = Field(ge=0, le=2)
    context_window_tokens: int = Field(gt=0)
    chat_max_tokens: int = Field(gt=0)
    structured_max_tokens: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_output_budgets(self) -> Self:
        if self.chat_max_tokens >= self.context_window_tokens:
            raise ValueError("chat_max_tokens must be less than context_window_tokens")
        if self.structured_max_tokens >= self.context_window_tokens:
            raise ValueError("structured_max_tokens must be less than context_window_tokens")
        return self


class ModelConfigurationResponse(SchemaBase):
    configured: bool
    id: str | None = None
    version: int | None = None
    provider: str | None = None
    api_mode: Literal["chat_completions", "responses"] | None = None
    base_url: str | None = None
    model_name: str | None = None
    input_price_per_million_usd: float | None = None
    output_price_per_million_usd: float | None = None
    temperature: float | None = None
    context_window_tokens: int | None = None
    chat_max_tokens: int | None = None
    structured_max_tokens: int | None = None
    api_key_hint: str | None = None
    validated_at: datetime | None = None
    created_at: datetime | None = None
    created_by_user_id: str | None = None
    created_by_email: str | None = None


class ModelConfigurationProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_mode: Literal["chat_completions", "responses"] = "chat_completions"
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


class ModelConfigurationUpdateRequest(
    ModelConfigurationProbeRequest,
    ModelRuntimeParameters,
):
    # Keep probe's explicit default separate from updates: legacy clients that
    # omit this field must not silently change an existing Responses config.
    api_mode: Literal["chat_completions", "responses"] | None = None
    model_name: str = Field(max_length=256)
    input_price_per_million_usd: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        le=MODEL_PRICE_MAX_USD,
        max_digits=MODEL_PRICE_PRECISION,
        decimal_places=MODEL_PRICE_SCALE,
    )
    output_price_per_million_usd: Decimal | None = Field(
        default=None,
        ge=Decimal("0"),
        le=MODEL_PRICE_MAX_USD,
        max_digits=MODEL_PRICE_PRECISION,
        decimal_places=MODEL_PRICE_SCALE,
    )
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
