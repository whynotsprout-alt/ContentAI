from __future__ import annotations

from datetime import datetime
from typing import Any

from models.base import new_id, utcnow
from models.enums import (
    ExecutionAttemptKind,
    ExecutionAttemptStatus,
    MessageRole,
    MessageType,
    RunStatus,
    SessionStatus,
    TitleSource,
    ToolExecutionStatus,
)
from sqlalchemy import Column, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class ChatSession(SQLModel, table=True):
    __tablename__ = "chatsession"
    __table_args__ = (
        Index("ix_chatsession_tenant_owner", "tenant_id", "owner_user_id"),
        Index("ix_chatsession_tenant_owner_updated", "tenant_id", "owner_user_id", "updated_at"),
        Index("ix_chatsession_agent_updated", "agent_id", "updated_at"),
    )

    id: str = Field(default_factory=lambda: new_id("ses"), primary_key=True)
    title: str = "New Session"
    title_source: TitleSource = Field(default=TitleSource.default, index=True)
    status: SessionStatus = Field(default=SessionStatus.active, index=True)
    agent_id: str = Field(index=True, foreign_key="agentprofile.id")
    agent_version_id: str = Field(index=True, foreign_key="agentversion.id")
    langgraph_thread_id: str = Field(default_factory=lambda: new_id("thr"), index=True)
    tenant_id: str = Field(index=True)
    owner_user_id: str = Field(index=True)
    pinned_at: datetime | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def touch_updated_at(self, at: datetime | None = None) -> None:
        self.updated_at = utcnow() if at is None else at


class AgentInvocation(SQLModel, table=True):
    __tablename__ = "agentinvocation"
    __table_args__ = (
        Index("ix_agentinvocation_session_created", "session_id", "created_at"),
        Index(
            "ix_agentinvocation_tenant_user_created",
            "tenant_id",
            "created_by_user_id",
            "created_at",
        ),
    )

    id: str = Field(default_factory=lambda: new_id("inv"), primary_key=True)
    session_id: str = Field(index=True, foreign_key="chatsession.id")
    agent_id: str = Field(index=True, foreign_key="agentprofile.id")
    user_message_id: str | None = Field(default=None, index=True, foreign_key="chatmessage.id")
    tenant_id: str = Field(index=True)
    created_by_user_id: str = Field(index=True)
    created_at: datetime = Field(default_factory=utcnow)


class AgentExecution(SQLModel, table=True):
    __tablename__ = "agentexecution"
    __table_args__ = (
        Index("ix_agentexecution_invocation_created", "invocation_id", "created_at"),
        Index("ix_agentexecution_invocation_status", "invocation_id", "status"),
        Index("ix_agentexecution_status_updated", "status", "updated_at"),
    )

    id: str = Field(default_factory=lambda: new_id("exe"), primary_key=True)
    invocation_id: str = Field(index=True, foreign_key="agentinvocation.id")
    agent_version_id: str = Field(index=True, foreign_key="agentversion.id")
    trace_id: str = Field(default_factory=lambda: new_id("trc"), index=True)
    latest_checkpoint_id: str | None = Field(default=None, index=True)
    status: RunStatus = Field(default=RunStatus.pending, index=True)
    error: str = ""
    interrupt_payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    resume_payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    started_at: datetime | None = None
    finished_at: datetime | None = None
    cancel_requested_at: datetime | None = Field(default=None, index=True)
    claimed_at: datetime | None = Field(default=None, index=True)
    heartbeat_at: datetime | None = Field(default=None, index=True)
    lease_expires_at: datetime | None = Field(default=None, index=True)
    worker_id: str | None = Field(default=None, index=True)
    attempt_count: int = Field(default=0)
    next_attempt_kind: ExecutionAttemptKind = Field(
        default=ExecutionAttemptKind.initial,
        index=True,
    )
    current_attempt_id: str | None = Field(default=None, index=True)
    first_event_at: datetime | None = Field(default=None, index=True)
    first_token_at: datetime | None = Field(default=None, index=True)
    streaming_degraded: bool = Field(default=False, index=True)
    streaming_degraded_at: datetime | None = Field(default=None, index=True)
    streaming_degraded_reason: str = ""
    postprocess_completed_at: datetime | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def touch_updated_at(self, at: datetime | None = None) -> None:
        self.updated_at = utcnow() if at is None else at


class AgentExecutionAttempt(SQLModel, table=True):
    __tablename__ = "agentexecutionattempt"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "ordinal",
            name="ux_agentexecutionattempt_execution_ordinal",
        ),
        Index("ix_agentexecutionattempt_execution_started", "execution_id", "started_at"),
    )

    id: str = Field(default_factory=lambda: new_id("att"), primary_key=True)
    execution_id: str = Field(index=True, foreign_key="agentexecution.id")
    ordinal: int = Field(index=True)
    kind: ExecutionAttemptKind = Field(default=ExecutionAttemptKind.initial, index=True)
    worker_id: str = Field(default="", index=True)
    status: ExecutionAttemptStatus = Field(default=ExecutionAttemptStatus.running, index=True)
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    first_event_at: datetime | None = None
    first_token_at: datetime | None = None


class ChatMessage(SQLModel, table=True):
    __tablename__ = "chatmessage"
    __table_args__ = (Index("ix_chatmessage_session_created_id", "session_id", "created_at", "id"),)

    id: str = Field(default_factory=lambda: new_id("msg"), primary_key=True)
    session_id: str = Field(index=True, foreign_key="chatsession.id")
    invocation_id: str | None = Field(default=None, index=True, foreign_key="agentinvocation.id")
    parent_message_id: str | None = Field(default=None, index=True, foreign_key="chatmessage.id")
    role: MessageRole
    message_type: MessageType = Field(default=MessageType.text)
    message_metadata: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
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
        Index("ix_toolexecution_execution_sequence", "execution_id", "sequence"),
        UniqueConstraint(
            "execution_id",
            "tool_call_id",
            name="ux_toolexecution_execution_tool_call",
        ),
    )

    id: str = Field(default_factory=lambda: new_id("tool"), primary_key=True)
    execution_id: str = Field(index=True, foreign_key="agentexecution.id")
    tool_name: str = Field(index=True)
    tool_version: str = ""
    tool_call_id: str | None = Field(default=None, index=True)
    sequence: int = Field(default=0, index=True)
    arguments: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    result: Any = Field(default=None, sa_column=Column(JSONB, nullable=True))
    arguments_hash: str = Field(default="", index=True)
    result_digest: str = Field(default="", index=True)
    status: ToolExecutionStatus = Field(default=ToolExecutionStatus.pending, index=True)
    error: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def touch_updated_at(self, at: datetime | None = None) -> None:
        self.updated_at = utcnow() if at is None else at


class AgentEvent(SQLModel, table=True):
    __tablename__ = "agentevent"
    __table_args__ = (
        UniqueConstraint("execution_id", "sequence", name="ux_agentevent_execution_sequence"),
        Index("ix_agentevent_execution_created", "execution_id", "created_at"),
        Index("ix_agentevent_execution_sequence", "execution_id", "sequence"),
        Index("ix_agentevent_type_created", "event_type", "created_at"),
    )

    id: str = Field(default_factory=lambda: new_id("evt"), primary_key=True)
    execution_id: str = Field(index=True, foreign_key="agentexecution.id")
    event_type: str = Field(index=True)
    sequence: int = Field(default=0, index=True)
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    created_at: datetime = Field(default_factory=utcnow)


class ExecutionOutbox(SQLModel, table=True):
    __tablename__ = "executionoutbox"
    __table_args__ = (
        Index("ix_executionoutbox_status_available", "status", "available_at"),
        Index("ix_executionoutbox_locked_until", "locked_until"),
        UniqueConstraint(
            "execution_id",
            "kind",
            name="ux_executionoutbox_execution_kind",
        ),
    )

    id: str = Field(default_factory=lambda: new_id("out"), primary_key=True)
    execution_id: str = Field(
        index=True,
        foreign_key="agentexecution.id",
        ondelete="CASCADE",
    )
    kind: str = Field(default="execute", index=True)
    request_id: str = Field(default="", index=True)
    status: str = Field(default="pending", index=True)
    attempts: int = Field(default=0)
    processing_attempts: int = Field(default=0)
    available_at: datetime = Field(default_factory=utcnow, index=True)
    locked_by: str | None = Field(default=None, index=True)
    locked_until: datetime | None = Field(default=None, index=True)
    published_at: datetime | None = Field(default=None, index=True)
    last_error: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ExecutionResumeRequest(SQLModel, table=True):
    """Durable, single-consumer input for a LangGraph interrupt."""

    __tablename__ = "executionresumerequest"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "interrupt_id",
            name="ux_executionresumerequest_execution_interrupt",
        ),
        Index("ix_executionresumerequest_status_created", "status", "created_at"),
    )

    id: str = Field(default_factory=lambda: new_id("res"), primary_key=True)
    execution_id: str = Field(
        index=True,
        foreign_key="agentexecution.id",
        ondelete="CASCADE",
    )
    interrupt_id: str = Field(index=True)
    tool_calls_hash: str = Field(default="", index=True)
    value: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    status: str = Field(default="pending", index=True)
    claimed_by: str | None = Field(default=None, index=True)
    claimed_at: datetime | None = Field(default=None, index=True)
    consumed_at: datetime | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
