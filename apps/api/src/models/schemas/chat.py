from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from models.enums import MessageRole, MessageType
from models.schemas.agent import AgentId
from models.schemas.base import InputSchemaBase, SchemaBase
from models.schemas.memory import ConversationMemory
from pydantic import Field, constr, field_validator, model_validator

SessionId = constr(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9_-]+$")
TitleText = constr(min_length=1, max_length=120, strip_whitespace=True)
MessageText = constr(min_length=1, max_length=8000, strip_whitespace=True)
IdempotencyKey = constr(min_length=1, max_length=255, strip_whitespace=True)


class AgentExecutionState(StrEnum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    waiting_input = "waiting_input"
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
    tool_call_id: str | None = None
    tool_name: constr(min_length=1, max_length=120)
    result: Any = None
    error: ErrorDetail | None = None


class CreateSessionRequest(InputSchemaBase):
    agent_id: AgentId


class CreateSessionResponse(SchemaBase):
    session_id: SessionId
    agent_id: AgentId
    agent_version_id: str
    tenant_id: str | None = None
    user_id: str | None = None
    title: TitleText


class ChatSessionSummary(SchemaBase):
    session_id: SessionId
    agent_id: AgentId
    agent_version_id: str
    title: TitleText
    created_at: datetime
    updated_at: datetime
    latest_execution_status: AgentExecutionState | None = None
    message_count: int = Field(default=0, ge=0)


class ChatRequest(InputSchemaBase):
    agent_id: AgentId
    message: MessageText
    message_id: SessionId | None = None
    idempotency_key: IdempotencyKey | None = None


class AgentMessageRequest(ChatRequest):
    session_id: SessionId


class ChatUserMessageResponse(SchemaBase):
    session_id: SessionId
    message_id: str
    execution_id: str
    status: AgentExecutionState
    trace_id: str | None = None
    error: ErrorDetail | None = None


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

    @model_validator(mode="after")
    def _validate_role_type_pair(self) -> "ChatMessageResponse":
        allowed = {
            MessageRole.user: {MessageType.text, MessageType.markdown, MessageType.json},
            MessageRole.assistant: {
                MessageType.text,
                MessageType.markdown,
                MessageType.json,
                MessageType.tool_call,
                MessageType.reasoning,
                MessageType.artifact,
                MessageType.citation,
                MessageType.image,
                MessageType.file,
            },
            MessageRole.tool: {MessageType.tool_result, MessageType.json, MessageType.text},
            MessageRole.system: {MessageType.text, MessageType.markdown, MessageType.json},
        }
        if self.message_type not in allowed[self.role]:
            raise ValueError(f"Invalid message_type {self.message_type!s} for role {self.role!s}")
        return self


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
    streaming_degraded: bool = False
    streaming_degraded_reason: str = ""
    queue_stage: Literal["dispatching", "waiting_worker", "starting"] | None = None


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

    @model_validator(mode="after")
    def _validate_cursor_mode(self) -> "MessageListRequest":
        if self.cursor is not None and self.before is not None:
            raise ValueError("cursor and before are mutually exclusive")
        return self


class ChatHistoryResponse(SchemaBase):
    messages: list[ChatMessageResponse] = Field(default_factory=list)
    next_cursor: str | None = None


class StreamEventV2(SchemaBase):
    type: Literal[
        "token", "tool_start", "tool_progress", "tool_end", "state", "error", "done", "heartbeat"
    ]
    execution_id: str | None = None
    session_id: str | None = None
    thread_id: str | None = None
    request_id: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class StreamEventV3(SchemaBase):
    schema_version: Literal[3] = 3
    execution_id: str
    sequence: int = Field(ge=1)
    event_id: str
    channel: Literal["messages", "tools", "values", "lifecycle", "interrupts", "errors"]
    namespace: tuple[str, ...] = ()
    attempt_id: str | None = None
    message_id: str | None = None
    tool_call_id: str | None = None
    timestamp: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class UserReplyRequest(InputSchemaBase):
    agent_id: AgentId
    message: MessageText
