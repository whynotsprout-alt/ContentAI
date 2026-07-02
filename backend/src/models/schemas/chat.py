from datetime import datetime

from models.enums import MessageRole, MessageType, RunStatus
from models.schemas.account import AccountId
from models.schemas.base import InputSchemaBase, SchemaBase
from models.schemas.memory import ConversationMemory
from pydantic import Field, constr

SessionId = constr(min_length=1, max_length=120, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
TitleText = constr(min_length=1, max_length=120, strip_whitespace=True)


class CreateSessionResponse(SchemaBase):
    session_id: SessionId
    title: TitleText


class ChatSessionSummary(SchemaBase):
    session_id: SessionId
    title: TitleText
    created_at: datetime
    updated_at: datetime
    latest_run_id: str | None = None
    latest_status: RunStatus | None = None
    message_count: int = Field(default=0, ge=0)


class RunCreateRequest(InputSchemaBase):
    session_id: SessionId | None = None
    account_id: AccountId
    message: constr(min_length=1, max_length=8000, strip_whitespace=True)


class ChatUserMessageCreate(InputSchemaBase):
    account_id: AccountId
    message: constr(min_length=1, max_length=8000, strip_whitespace=True)


class ChatUserMessageResponse(SchemaBase):
    session_id: SessionId
    message_id: str
    run_id: str
    status: RunStatus
    memory: ConversationMemory | None = None
    error: str = ""


class ChatMessageResponse(SchemaBase):
    id: str
    role: MessageRole
    message_type: MessageType
    content: constr(min_length=1, max_length=240000)
    run_id: str | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    parent_message_id: str | None = None
    created_at: datetime


class ChatSessionDetail(ChatSessionSummary):
    messages: list[ChatMessageResponse] = Field(default_factory=list)
    memory: ConversationMemory = Field(default_factory=ConversationMemory)


class RunResponse(SchemaBase):
    id: str
    session_id: SessionId
    account_id: str
    user_message_id: str | None = None
    user_message: constr(min_length=1, max_length=8000, strip_whitespace=True)
    status: RunStatus
    error: str = Field(default="", max_length=4000)
    messages: list[ChatMessageResponse] = Field(default_factory=list)
    memory: ConversationMemory = Field(default_factory=ConversationMemory)
