from __future__ import annotations

from datetime import datetime

from pydantic import EmailStr, Field, field_validator

from contentai.models.schemas.base import InputSchemaBase, SchemaBase


class RegisterRequest(InputSchemaBase):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)


class LoginRequest(InputSchemaBase):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordRequest(InputSchemaBase):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=10, max_length=128)

    @field_validator("new_password")
    @classmethod
    def passwords_must_differ(cls, value: str, info):
        if value == info.data.get("current_password"):
            raise ValueError("new password must differ from current password")
        return value


class CurrentUserResponse(SchemaBase):
    id: str
    email: EmailStr
    role: str
    status: str
    email_verified_at: datetime | None = None
    password_changed_at: datetime
    created_at: datetime
    last_login_at: datetime | None = None
    must_change_password: bool = False
    temporary_password_expires_at: datetime | None = None


class MessageResponse(SchemaBase):
    message: str


class AdminUserSummary(CurrentUserResponse):
    password_set: bool = True
    agent_count: int = 0
    conversation_count: int = 0
    # These totals include provider-backed internal calls such as titles and memory.
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    input_cost_usd: float = 0
    output_cost_usd: float = 0
    total_cost_usd: float = 0
    # User-visible agent turns are exposed separately from internal processing.
    chat_input_tokens: int = 0
    chat_output_tokens: int = 0
    chat_total_tokens: int = 0
    chat_input_cost_usd: float = 0
    chat_output_cost_usd: float = 0
    chat_total_cost_usd: float = 0
    background_input_tokens: int = 0
    background_output_tokens: int = 0
    background_total_tokens: int = 0
    background_input_cost_usd: float = 0
    background_output_cost_usd: float = 0
    background_total_cost_usd: float = 0
    usage_call_count: int = 0
    completed_usage_call_count: int = 0
    missing_usage_call_count: int = 0
    failed_usage_call_count: int = 0
    usage_coverage: float = 1.0


class AdminUserListResponse(SchemaBase):
    items: list[AdminUserSummary]
    next_cursor: str | None = None
