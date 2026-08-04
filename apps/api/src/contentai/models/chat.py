from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, Relationship, SQLModel

from contentai.models.base import new_id, utcnow
from contentai.models.enums import (
    ExecutionAttemptKind,
    ExecutionAttemptStatus,
    MessageRole,
    MessageType,
    RunStatus,
    ToolExecutionStatus,
)

if TYPE_CHECKING:
    from contentai.models.model_configuration import ModelConfiguration


class ChatSession(SQLModel, table=True):
    __tablename__ = "chatsession"
    __table_args__ = (
        Index("ux_chatsession_id_user", "id", "user_id", unique=True),
        Index("ux_chatsession_id_agent_user", "id", "agent_id", "user_id", unique=True),
        Index("ux_chatsession_id_agent_version", "id", "agent_version_id", unique=True),
        ForeignKeyConstraint(
            ["agent_id", "user_id"],
            ["agentprofile.id", "agentprofile.user_id"],
            name="fk_chatsession_agent_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["agent_version_id", "agent_id"],
            ["agentversion.id", "agentversion.agent_id"],
            name="fk_chatsession_version_agent",
            ondelete="RESTRICT",
        ),
        Index("ix_chatsession_user_updated", "user_id", "updated_at"),
        Index("ix_chatsession_user_updated_id", "user_id", "updated_at", "id"),
        Index("ix_chatsession_agent_updated", "agent_id", "updated_at"),
    )

    id: str = Field(default_factory=lambda: new_id("ses"), primary_key=True)
    title: str = "New Session"
    agent_id: str = Field(index=True, foreign_key="agentprofile.id", ondelete="RESTRICT")
    agent_version_id: str = Field(index=True, foreign_key="agentversion.id", ondelete="RESTRICT")
    langgraph_thread_id: str = Field(
        default_factory=lambda: new_id("thr"),
        index=True,
        unique=True,
    )
    user_id: str = Field(index=True, foreign_key="appuser.id", ondelete="CASCADE")
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))

    def touch_updated_at(self, at: datetime | None = None) -> None:
        self.updated_at = utcnow() if at is None else at


class AgentInvocation(SQLModel, table=True):
    __tablename__ = "agentinvocation"
    __table_args__ = (
        Index("ix_agentinvocation_session_created", "session_id", "created_at"),
        Index("ix_agentinvocation_user_created", "user_id", "created_at"),
        UniqueConstraint(
            "session_id",
            "idempotency_key",
            name="ux_agentinvocation_session_idempotency",
        ),
        Index("ux_agentinvocation_id_session", "id", "session_id", unique=True),
        ForeignKeyConstraint(
            ["session_id", "agent_id", "user_id"],
            ["chatsession.id", "chatsession.agent_id", "chatsession.user_id"],
            name="fk_agentinvocation_session_lineage",
            ondelete="CASCADE",
        ),
    )

    id: str = Field(default_factory=lambda: new_id("inv"), primary_key=True)
    session_id: str = Field(index=True, foreign_key="chatsession.id", ondelete="CASCADE")
    agent_id: str = Field(index=True, foreign_key="agentprofile.id", ondelete="RESTRICT")
    user_id: str = Field(index=True, foreign_key="appuser.id", ondelete="CASCADE")
    idempotency_key: str | None = Field(default=None)
    request_sha256: str | None = Field(default=None, sa_type=String(64))
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))


class AgentExecution(SQLModel, table=True):
    __tablename__ = "agentexecution"
    __table_args__ = (
        Index("ux_agentexecution_invocation", "invocation_id", unique=True),
        Index(
            "ux_agentexecution_id_model_config",
            "id",
            "model_config_id",
            unique=True,
        ),
        Index(
            "ux_agentexecution_id_session_version",
            "id",
            "session_id",
            "agent_version_id",
            unique=True,
        ),
        ForeignKeyConstraint(
            ["invocation_id", "session_id"],
            ["agentinvocation.id", "agentinvocation.session_id"],
            name="fk_agentexecution_invocation_session",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["session_id", "agent_version_id"],
            ["chatsession.id", "chatsession.agent_version_id"],
            name="fk_agentexecution_session_version",
            ondelete="CASCADE",
        ),
        Index("ix_agentexecution_invocation_created", "invocation_id", "created_at"),
        Index("ix_agentexecution_invocation_status", "invocation_id", "status"),
        Index("ix_agentexecution_status_updated", "status", "updated_at"),
        Index("ix_agentexecution_session_updated_id", "session_id", "updated_at", "id"),
    )

    id: str = Field(default_factory=lambda: new_id("exe"), primary_key=True)
    invocation_id: str = Field(
        index=True,
        foreign_key="agentinvocation.id",
        ondelete="CASCADE",
    )
    session_id: str = Field(index=True)
    agent_version_id: str = Field(index=True, foreign_key="agentversion.id", ondelete="RESTRICT")
    model_config_id: str = Field(
        index=True,
        foreign_key="modelconfiguration.id",
        ondelete="RESTRICT",
    )
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
    started_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    finished_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    cancel_requested_at: datetime | None = Field(
        default=None, index=True, sa_type=DateTime(timezone=True)
    )
    claimed_at: datetime | None = Field(
        default=None, index=True, sa_type=DateTime(timezone=True)
    )
    heartbeat_at: datetime | None = Field(
        default=None, index=True, sa_type=DateTime(timezone=True)
    )
    lease_expires_at: datetime | None = Field(
        default=None, index=True, sa_type=DateTime(timezone=True)
    )
    worker_id: str | None = Field(default=None, index=True)
    attempt_count: int = Field(default=0)
    next_attempt_kind: ExecutionAttemptKind = Field(
        default=ExecutionAttemptKind.initial,
        index=True,
    )
    current_attempt_id: str | None = Field(default=None, index=True)
    first_event_at: datetime | None = Field(
        default=None, index=True, sa_type=DateTime(timezone=True)
    )
    first_token_at: datetime | None = Field(
        default=None, index=True, sa_type=DateTime(timezone=True)
    )
    streaming_degraded: bool = Field(default=False, index=True)
    streaming_degraded_at: datetime | None = Field(
        default=None, index=True, sa_type=DateTime(timezone=True)
    )
    streaming_degraded_reason: str = ""
    postprocess_completed_at: datetime | None = Field(
        default=None, index=True, sa_type=DateTime(timezone=True)
    )
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))

    model_configuration: ModelConfiguration = Relationship(back_populates="executions")

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
    execution_id: str = Field(index=True, foreign_key="agentexecution.id", ondelete="CASCADE")
    ordinal: int = Field(index=True)
    kind: ExecutionAttemptKind = Field(default=ExecutionAttemptKind.initial, index=True)
    worker_id: str = Field(default="", index=True)
    status: ExecutionAttemptStatus = Field(default=ExecutionAttemptStatus.running, index=True)
    started_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))
    finished_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    first_event_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    first_token_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))


class ChatMessage(SQLModel, table=True):
    __tablename__ = "chatmessage"
    __table_args__ = (
        Index("ix_chatmessage_session_created_id", "session_id", "created_at", "id"),
        Index(
            "ux_chatmessage_assistant_execution",
            "execution_id",
            unique=True,
            postgresql_where=text("role = 'assistant' AND execution_id IS NOT NULL"),
        ),
    )

    id: str = Field(default_factory=lambda: new_id("msg"), primary_key=True)
    session_id: str = Field(index=True, foreign_key="chatsession.id", ondelete="CASCADE")
    invocation_id: str | None = Field(
        default=None,
        index=True,
        foreign_key="agentinvocation.id",
        ondelete="CASCADE",
    )
    execution_id: str | None = Field(
        default=None,
        index=True,
        foreign_key="agentexecution.id",
        ondelete="CASCADE",
    )
    role: MessageRole
    message_type: MessageType = Field(default=MessageType.text)
    content: str
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))


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
    execution_id: str = Field(index=True, foreign_key="agentexecution.id", ondelete="CASCADE")
    tool_name: str = Field(index=True)
    tool_version: str = ""
    tool_call_id: str | None = Field(default=None, index=True)
    sequence: int = Field(default=0, index=True)
    arguments_hash: str = Field(default="", index=True)
    result_digest: str = Field(default="", index=True)
    status: ToolExecutionStatus = Field(default=ToolExecutionStatus.pending, index=True)
    error: str = ""
    started_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    finished_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    duration_ms: int | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))

    def touch_updated_at(self, at: datetime | None = None) -> None:
        self.updated_at = utcnow() if at is None else at


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
        ForeignKeyConstraint(
            ["execution_id", "model_config_id"],
            ["agentexecution.id", "agentexecution.model_config_id"],
            name="fk_executionoutbox_execution_model_config",
            ondelete="CASCADE",
        ),
    )

    id: str = Field(default_factory=lambda: new_id("out"), primary_key=True)
    execution_id: str = Field(
        index=True,
        foreign_key="agentexecution.id",
        ondelete="CASCADE",
    )
    model_config_id: str = Field(
        index=True,
        foreign_key="modelconfiguration.id",
        ondelete="RESTRICT",
    )
    kind: str = Field(default="execute", index=True)
    request_id: str = Field(default="", index=True)
    payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    status: str = Field(default="pending", index=True)
    attempts: int = Field(default=0)
    processing_attempts: int = Field(default=0)
    available_at: datetime = Field(
        default_factory=utcnow, index=True, sa_type=DateTime(timezone=True)
    )
    locked_by: str | None = Field(default=None, index=True)
    locked_until: datetime | None = Field(
        default=None, index=True, sa_type=DateTime(timezone=True)
    )
    published_at: datetime | None = Field(
        default=None, index=True, sa_type=DateTime(timezone=True)
    )
    last_error: str = ""
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))


class ExecutionResumeRequest(SQLModel, table=True):
    """Durable, single-consumer input for a LangGraph interrupt."""

    __tablename__ = "executionresumerequest"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "interrupt_id",
            name="ux_executionresumerequest_execution_interrupt",
        ),
        Index("ux_executionresumerequest_message", "message_id", unique=True),
        CheckConstraint(
            "decision IS NULL OR decision IN ('approve', 'reject')",
            name="ck_executionresumerequest_decision",
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
    decision: str | None = Field(default=None, index=True)
    message_id: str | None = Field(
        default=None,
        index=True,
        foreign_key="chatmessage.id",
        ondelete="CASCADE",
    )
    tool_calls_hash: str = Field(default="", index=True)
    value: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    status: str = Field(default="pending", index=True)
    claimed_by: str | None = Field(default=None, index=True)
    claimed_at: datetime | None = Field(
        default=None, index=True, sa_type=DateTime(timezone=True)
    )
    consumed_at: datetime | None = Field(
        default=None, index=True, sa_type=DateTime(timezone=True)
    )
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))


class ServiceHeartbeat(SQLModel, table=True):
    __tablename__ = "serviceheartbeat"
    __table_args__ = (
        UniqueConstraint(
            "service_name",
            "instance_id",
            "queue_name",
            name="ux_serviceheartbeat_service_instance_queue",
        ),
        Index(
            "ix_serviceheartbeat_service_queue_heartbeat",
            "service_name",
            "queue_name",
            "heartbeat_at",
        ),
    )

    id: str = Field(default_factory=lambda: new_id("hbt"), primary_key=True)
    service_name: str = Field(index=True)
    instance_id: str = Field(index=True)
    queue_name: str = Field(default="", index=True)
    status: str = Field(default="healthy", index=True)
    detail: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    heartbeat_at: datetime = Field(
        default_factory=utcnow, index=True, sa_type=DateTime(timezone=True)
    )
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))


class CheckpointDeletionOutbox(SQLModel, table=True):
    __tablename__ = "checkpointdeletionoutbox"
    __table_args__ = (
        UniqueConstraint("thread_id", "checkpoint_ns", name="ux_checkpointdeletion_thread_ns"),
        Index("ix_checkpointdeletion_status_available", "status", "available_at"),
    )

    id: str = Field(default_factory=lambda: new_id("cdo"), primary_key=True)
    user_id: str = Field(index=True, foreign_key="appuser.id", ondelete="CASCADE")
    session_id: str = Field(index=True)
    thread_id: str = Field(index=True)
    checkpoint_ns: str = Field(default="", index=True)
    status: str = Field(default="pending", index=True)
    attempts: int = Field(default=0)
    available_at: datetime = Field(
        default_factory=utcnow, index=True, sa_type=DateTime(timezone=True)
    )
    locked_by: str | None = Field(default=None, index=True)
    locked_until: datetime | None = Field(
        default=None, index=True, sa_type=DateTime(timezone=True)
    )
    last_error: str = Field(default="")
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))


class SideEffectReceipt(SQLModel, table=True):
    __tablename__ = "sideeffectreceipt"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "tool_call_id",
            name="ux_sideeffectreceipt_execution_tool_call",
        ),
        Index("ix_sideeffectreceipt_status_created", "status", "created_at"),
    )

    id: str = Field(default_factory=lambda: new_id("sfx"), primary_key=True)
    execution_id: str = Field(index=True, foreign_key="agentexecution.id", ondelete="CASCADE")
    tool_call_id: str = Field(index=True)
    idempotency_key: str = Field(default="", index=True)
    operation: str = Field(index=True)
    status: str = Field(default="completed", index=True)
    result_digest: str = Field(default="", index=True)
    detail: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))
