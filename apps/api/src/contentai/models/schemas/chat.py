from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, constr, field_serializer, field_validator

from contentai.models.enums import MessageRole, MessageType
from contentai.models.schemas.agent import AgentId
from contentai.models.schemas.base import InputSchemaBase, SchemaBase
from contentai.services.event_stream import MAX_SAFE_EVENT_SEQUENCE

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


class ErrorDetail(SchemaBase):
    code: constr(min_length=1, max_length=80, strip_whitespace=True)
    message: constr(min_length=1, max_length=4000, strip_whitespace=True)
    retryable: bool = False


class CreateSessionRequest(InputSchemaBase):
    agent_id: AgentId


class CreateSessionResponse(SchemaBase):
    session_id: SessionId
    agent_id: AgentId
    agent_version_id: str
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


class ChatSessionListResponse(SchemaBase):
    items: list[ChatSessionSummary] = Field(default_factory=list)
    next_cursor: str | None = None


class ChatRequest(InputSchemaBase):
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
    created_at: datetime


class ChatExecutionResponse(SchemaBase):
    id: str
    session_id: SessionId
    status: AgentExecutionState
    trace_id: str | None = None
    error: ErrorDetail | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    interrupt: "PublicInterrupt | None" = None
    streaming_degraded: bool = False
    streaming_degraded_reason: str = ""
    queue_stage: Literal["dispatching", "waiting_worker", "starting"] | None = None


class ChatSessionDetail(ChatSessionSummary):
    messages: list[ChatMessageResponse] = Field(default_factory=list)
    next_cursor: str | None = None
    latest_execution: ChatExecutionResponse | None = None


class MessageListRequest(InputSchemaBase):
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None

    @field_validator("cursor", mode="before")
    @classmethod
    def _validate_cursor(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Cursor must be a string")
        normalized = value.strip()
        if not normalized:
            raise ValueError("Cursor cannot be empty")
        # API responses use signed v1 cursors. The request schema cannot verify
        # their signature because the scope and signer belong to the service
        # layer, but it should allow the cursor to reach that validation path.
        if normalized.startswith("v1."):
            parts = normalized.split(".")
            if len(parts) != 3 or any(not part for part in parts[1:]):
                raise ValueError("Cursor format is invalid")
            return normalized
        if "|" not in normalized:
            raise ValueError("Cursor format is invalid")
        created_at_raw, message_id = normalized.split("|", 1)
        if not created_at_raw or not message_id:
            raise ValueError("Cursor format is invalid")
        try:
            parsed = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Cursor timestamp is invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Cursor timestamp must be timezone-aware")
        return normalized



class _StreamSchemaBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class StreamErrorDataV3(_StreamSchemaBase):
    model_config = ConfigDict(from_attributes=True, extra="allow")

    code: constr(min_length=1, max_length=80, strip_whitespace=True)
    message: constr(min_length=1, max_length=4000, strip_whitespace=True)
    name: constr(min_length=1, max_length=120, strip_whitespace=True) | None = None
    retryable: bool | None = None


class _StreamEventV3Base(_StreamSchemaBase):
    schema_version: Literal[3] = 3
    execution_id: constr(min_length=1, strip_whitespace=True)
    sequence: int = Field(ge=1, le=MAX_SAFE_EVENT_SEQUENCE)
    event_id: constr(min_length=1, strip_whitespace=True)
    channel: str
    namespace: tuple[str, ...] = ()
    attempt_id: constr(min_length=1, strip_whitespace=True) | None = None
    message_id: constr(min_length=1, strip_whitespace=True) | None = None
    tool_call_id: constr(min_length=1, strip_whitespace=True) | None = None
    timestamp: datetime
    data: Any

    @field_serializer("timestamp", when_used="json")
    def serialize_utc_timestamp(self, value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("API datetimes must be timezone-aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class StreamPublicEventV3(_StreamEventV3Base):
    channel: Literal["messages", "tools", "values", "lifecycle", "interrupts"]
    data: dict[str, Any] = Field(default_factory=dict)


class StreamErrorEventV3(_StreamEventV3Base):
    channel: Literal["errors"]
    data: StreamErrorDataV3


StreamEventV3 = Annotated[
    StreamPublicEventV3 | StreamErrorEventV3,
    Field(discriminator="channel"),
]


class PublicMemoryProposal(SchemaBase):
    type: constr(min_length=1, max_length=80, strip_whitespace=True)
    content: constr(min_length=1, max_length=8000, strip_whitespace=True)


class PublicInterruptAction(SchemaBase):
    tool_name: constr(min_length=1, max_length=255, strip_whitespace=True)
    purpose: constr(min_length=1, max_length=1000, strip_whitespace=True)
    memory: PublicMemoryProposal | None = None


class PublicInterrupt(SchemaBase):
    interrupt_id: constr(min_length=1, max_length=255, strip_whitespace=True)
    actions: list[PublicInterruptAction] = Field(min_length=1)


class UserReplyRequest(InputSchemaBase):
    interrupt_id: constr(min_length=1, max_length=255, strip_whitespace=True)
    decision: Literal["approve", "reject"]
