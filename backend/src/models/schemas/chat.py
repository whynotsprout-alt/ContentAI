from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from models.enums import MessageRole, MessageType, RunStatus
from models.schemas.account import AccountId
from models.schemas.base import InputSchemaBase, SchemaBase
from models.schemas.memory import ConversationMemory
from pydantic import Field, constr, field_validator, model_validator

SessionId = constr(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9_-]+$")
TitleText = constr(min_length=1, max_length=120, strip_whitespace=True)
MessageText = constr(min_length=1, max_length=8000, strip_whitespace=True)


class AgentExecutionState(StrEnum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    interrupted = "interrupted"


class MessageState(StrEnum):
    pending = "pending"
    streaming = "streaming"
    completed = "completed"
    failed = "failed"


class ErrorDetail(SchemaBase):
    code: constr(min_length=1, max_length=80, strip_whitespace=True)
    message: constr(min_length=1, max_length=4000, strip_whitespace=True)
    retryable: bool = False


class MessageCitation(SchemaBase):
    source: constr(min_length=1, max_length=300)
    url: str | None = None


class ToolExecutionResult(SchemaBase):
    tool_name: constr(min_length=1, max_length=120)
    result: Any = None
    error: ErrorDetail | None = None


class CreateSessionRequest(InputSchemaBase):
    account_id: AccountId
    tenant_id: str | None = None
    user_id: str | None = None


class CreateSessionResponse(SchemaBase):
    session_id: SessionId
    account_id: AccountId
    tenant_id: str | None = None
    user_id: str | None = None
    title: TitleText


def _normalize_execution_state(value: Any) -> AgentExecutionState | None:
    if value is None:
        return None
    if isinstance(value, AgentExecutionState):
        return value
    if isinstance(value, RunStatus):
        value = value.value
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized in {"running", "processing"}:
        return AgentExecutionState.running
    if normalized == "interrupted":
        return AgentExecutionState.interrupted
    if normalized == "pending":
        return AgentExecutionState.pending
    if normalized in {"completed", "done", "success"}:
        return AgentExecutionState.completed
    if normalized in {"failed", "cancelled", "error"}:
        return AgentExecutionState.failed
    return AgentExecutionState.running


def _normalize_error(value: Any) -> ErrorDetail | None:
    if value is None or value == "":
        return None
    if isinstance(value, ErrorDetail):
        return value
    if isinstance(value, dict):
        code = value.get("code", "EXECUTION_ERROR")
        message = value.get("message", "")
        retryable = bool(value.get("retryable", False))
        msg = str(message).strip()
        if not msg:
            return None
        return ErrorDetail(
            code=str(code).strip() or "EXECUTION_ERROR",
            message=msg,
            retryable=retryable,
        )
    return ErrorDetail(code="EXECUTION_ERROR", message=str(value).strip(), retryable=False)


class ChatSessionSummary(SchemaBase):
    session_id: SessionId
    account_id: AccountId
    title: TitleText
    created_at: datetime
    updated_at: datetime
    status: AgentExecutionState | None = None
    latest_status: AgentExecutionState | None = None
    message_count: int = Field(default=0, ge=0)

    @field_validator("status", "latest_status", mode="before")
    @classmethod
    def _normalize_status(cls, value: Any) -> AgentExecutionState | None:
        return _normalize_execution_state(value)

    @model_validator(mode="after")
    def _sync_status(self) -> "ChatSessionSummary":
        if self.status is None and self.latest_status is not None:
            self.status = self.latest_status
        elif self.latest_status is None and self.status is not None:
            self.latest_status = self.status
        return self


class ChatRequest(InputSchemaBase):
    message: MessageText


class AgentMessageRequest(ChatRequest):
    session_id: SessionId


class ChatUserMessageResponse(SchemaBase):
    session_id: SessionId
    message_id: str
    status: AgentExecutionState
    trace_id: str | None = None
    error: ErrorDetail | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, value: Any) -> AgentExecutionState:
        return _normalize_execution_state(value) or AgentExecutionState.running

    @field_validator("error", mode="before")
    @classmethod
    def _coerce_error(cls, value: Any) -> ErrorDetail | None:
        return _normalize_error(value)


class ChatMessageResponse(SchemaBase):
    id: str
    role: MessageRole
    message_type: MessageType
    content: constr(min_length=1, max_length=240000)
    status: MessageState = MessageState.completed
    tool_name: str | None = None
    tool_call_id: str | None = None
    model_name: str | None = None
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    latency_ms: int | None = Field(default=None, ge=0)
    trace_id: str | None = None
    citations: list[MessageCitation] = Field(default_factory=list)
    tool_results: list[ToolExecutionResult] = Field(default_factory=list)
    created_at: datetime

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_message_status(cls, value: Any) -> MessageState:
        normalized = _normalize_execution_state(value)
        if normalized == AgentExecutionState.running:
            return MessageState.streaming
        if normalized == AgentExecutionState.failed:
            return MessageState.failed
        if normalized == AgentExecutionState.completed:
            return MessageState.completed
        if normalized == AgentExecutionState.pending:
            return MessageState.pending
        return MessageState.completed


class ChatExecutionResponse(SchemaBase):
    id: str
    session_id: SessionId
    status: AgentExecutionState
    trace_id: str | None = None
    error: ErrorDetail | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    interrupt_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, value: Any) -> AgentExecutionState:
        return _normalize_execution_state(value) or AgentExecutionState.running

    @field_validator("error", mode="before")
    @classmethod
    def _coerce_error(cls, value: Any) -> ErrorDetail | None:
        return _normalize_error(value)


class ChatSessionDetail(ChatSessionSummary):
    messages: list[ChatMessageResponse] = Field(default_factory=list)
    next_cursor: str | None = None
    memory: ConversationMemory = Field(default_factory=ConversationMemory)
    latest_execution: ChatExecutionResponse | None = None


class ExecutionResponse(ChatExecutionResponse):
    messages: list[ChatMessageResponse] = Field(default_factory=list)
    memory: ConversationMemory = Field(default_factory=ConversationMemory)


class MessageListRequest(InputSchemaBase):
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None
    before: str | None = None

    @field_validator("cursor", "before", mode="before")
    @classmethod
    def _validate_cursor(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Cursor must be a string")
        normalized = value.strip()
        if not normalized:
            raise ValueError("Cursor cannot be empty")
        if "|" not in normalized:
            raise ValueError("Cursor format is invalid")
        created_at_raw, message_id = normalized.split("|", 1)
        if not created_at_raw or not message_id:
            raise ValueError("Cursor format is invalid")
        try:
            datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Cursor timestamp is invalid") from exc
        return normalized


class ChatHistoryResponse(SchemaBase):
    messages: list[ChatMessageResponse] = Field(default_factory=list)
    next_cursor: str | None = None


class StreamEventV2(SchemaBase):
    type: Literal["token", "tool"]
    content: str = ""
    name: str | None = None


class UserReplyRequest(InputSchemaBase):
    message: MessageText
