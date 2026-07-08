from __future__ import annotations

from datetime import datetime
from typing import Any

from models.base import new_id, utcnow
from models.enums import RunStatus, ToolExecutionStatus
from sqlalchemy import Column, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class ChatSession(SQLModel, table=True):
    __tablename__ = "chatsession"
    __table_args__ = (
        Index("ix_chatsession_tenant_owner_updated", "tenant_id", "owner_user_id", "updated_at"),
        Index("ix_chatsession_account_updated", "account_id", "updated_at"),
    )

    id: str = Field(default_factory=lambda: new_id("ses"), primary_key=True)
    title: str = "New Session"
    account_id: str = Field(index=True, foreign_key="account.id")
    langgraph_thread_id: str = Field(default_factory=lambda: new_id("thr"), index=True)
    tenant_id: str = Field(index=True)
    owner_user_id: str = Field(index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def touch_updated_at(self, at: datetime | None = None) -> None:
        self.updated_at = utcnow() if at is None else at


class AgentInvocation(SQLModel, table=True):
    __tablename__ = "agentinvocation"
    __table_args__ = (
        Index("ix_agentinvocation_session_created", "session_id", "created_at"),
        Index("ix_agentinvocation_tenant_user_created", "tenant_id", "created_by_user_id", "created_at"),
    )

    id: str = Field(default_factory=lambda: new_id("inv"), primary_key=True)
    session_id: str = Field(index=True, foreign_key="chatsession.id")
    account_id: str = Field(index=True, foreign_key="account.id")
    user_message_id: str | None = Field(default=None, index=True, foreign_key="chatmessage.id")
    tenant_id: str = Field(index=True)
    created_by_user_id: str = Field(index=True)
    created_at: datetime = Field(default_factory=utcnow)


class AgentExecution(SQLModel, table=True):
    __tablename__ = "agentexecution"
    __table_args__ = (
        Index("ix_agentexecution_invocation_created", "invocation_id", "created_at"),
        Index("ix_agentexecution_status_updated", "status", "updated_at"),
    )

    id: str = Field(default_factory=lambda: new_id("exe"), primary_key=True)
    invocation_id: str = Field(index=True, foreign_key="agentinvocation.id")
    checkpoint_id: str | None = Field(default=None, index=True)
    status: RunStatus = Field(default=RunStatus.running, index=True)
    error: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested_at: datetime | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def touch_updated_at(self, at: datetime | None = None) -> None:
        self.updated_at = utcnow() if at is None else at


class ChatMessage(SQLModel, table=True):
    __tablename__ = "chatmessage"
    __table_args__ = (
        Index("ix_chatmessage_session_created", "session_id", "created_at"),
    )

    id: str = Field(default_factory=lambda: new_id("msg"), primary_key=True)
    session_id: str = Field(index=True, foreign_key="chatsession.id")
    invocation_id: str | None = Field(default=None, index=True, foreign_key="agentinvocation.id")
    role: str
    message_type: str = Field(default="text")
    message_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    content: str
    tool_name: str | None = Field(default=None, index=True)
    tool_call_id: str | None = Field(default=None, index=True)
    model_name: str | None = Field(default=None, index=True)
    input_tokens: int = Field(default=0)
    output_tokens: int = Field(default=0)
    latency_ms: int | None = None
    created_at: datetime = Field(default_factory=utcnow)


class ToolExecution(SQLModel, table=True):
    __tablename__ = "toolexecution"
    __table_args__ = (
        Index("ix_toolexecution_execution_created", "execution_id", "created_at"),
        Index("ix_toolexecution_tool_created", "tool_name", "created_at"),
    )

    id: str = Field(default_factory=lambda: new_id("tool"), primary_key=True)
    execution_id: str = Field(index=True, foreign_key="agentexecution.id")
    tool_name: str = Field(index=True)
    tool_call_id: str | None = Field(default=None, index=True)
    arguments: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    result: Any = Field(default=None, sa_column=Column(JSONB, nullable=True))
    status: ToolExecutionStatus = Field(default=ToolExecutionStatus.running, index=True)
    error: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def touch_updated_at(self, at: datetime | None = None) -> None:
        self.updated_at = utcnow() if at is None else at
