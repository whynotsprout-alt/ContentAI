from datetime import datetime

from models.base import json_dumps, new_id, utcnow
from models.enums import MessageRole, MessageType, RunStatus
from sqlmodel import Field, SQLModel


class ChatSession(SQLModel, table=True):
    __tablename__ = "chatsession"

    id: str = Field(default_factory=lambda: new_id("ses"), primary_key=True)
    title: str = "New Session"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def touch_updated_at(self, at: datetime | None = None) -> None:
        self.updated_at = utcnow() if at is None else at


class AgentRun(SQLModel, table=True):
    __tablename__ = "agentrun"

    id: str = Field(default_factory=lambda: new_id("run"), primary_key=True)
    session_id: str = Field(index=True, foreign_key="chatsession.id")
    account_id: str = Field(index=True, foreign_key="account.id")
    user_message_id: str | None = Field(default=None)
    user_message: str
    status: RunStatus = Field(default=RunStatus.queued, index=True)
    error: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def touch_updated_at(self, at: datetime | None = None) -> None:
        self.updated_at = utcnow() if at is None else at


class ChatMessage(SQLModel, table=True):
    __tablename__ = "chatmessage"

    id: str = Field(default_factory=lambda: new_id("msg"), primary_key=True)
    session_id: str = Field(index=True, foreign_key="chatsession.id")
    role: MessageRole
    message_type: MessageType = Field(default=MessageType.text)
    message_metadata: str = Field(default_factory=lambda: json_dumps({}))
    content: str
    run_id: str | None = Field(default=None, index=True, foreign_key="agentrun.id")
    tool_name: str | None = Field(default=None, index=True)
    tool_call_id: str | None = Field(default=None, index=True)
    parent_message_id: str | None = Field(default=None, foreign_key="chatmessage.id")
    created_at: datetime = Field(default_factory=utcnow)


class AgentRunEvent(SQLModel, table=True):
    __tablename__ = "agentrunevent"

    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(index=True, foreign_key="agentrun.id")
    event: str
    payload: str = Field(default_factory=lambda: json_dumps({}))
    created_at: datetime = Field(default_factory=utcnow)
