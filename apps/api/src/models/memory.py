from __future__ import annotations

from datetime import datetime
from typing import Any

from models.base import new_id, utcnow
from models.enums import MemoryKind, MemorySourceType
from sqlalchemy import CheckConstraint, Column, Index, event, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class MemoryRecord(SQLModel, table=True):
    __tablename__ = "memoryrecord"
    __table_args__ = (
        Index(
            "ix_memoryrecord_lookup",
            "user_id",
            "agent_id",
            "session_id",
            "kind",
            "updated_at",
        ),
        Index(
            "ux_memoryrecord_active_agent",
            "user_id",
            "agent_id",
            "memory_key",
            unique=True,
            postgresql_where=text("agent_id IS NOT NULL AND deleted_at IS NULL"),
        ),
        Index(
            "ux_memoryrecord_active_session",
            "user_id",
            "session_id",
            "memory_key",
            unique=True,
            postgresql_where=text("session_id IS NOT NULL AND deleted_at IS NULL"),
        ),
        CheckConstraint(
            "(agent_id IS NOT NULL AND session_id IS NULL) OR "
            "(agent_id IS NULL AND session_id IS NOT NULL)",
            name="ck_memoryrecord_owner",
        ),
    )

    id: str = Field(default_factory=lambda: new_id("mem"), primary_key=True)
    user_id: str = Field(index=True, foreign_key="appuser.id", ondelete="CASCADE")
    agent_id: str | None = Field(
        default=None,
        index=True,
        foreign_key="agentprofile.id",
        ondelete="CASCADE",
    )
    session_id: str | None = Field(
        default=None,
        index=True,
        foreign_key="chatsession.id",
        ondelete="CASCADE",
    )
    memory_key: str = Field(index=True)
    kind: MemoryKind = Field(default=MemoryKind.semantic, index=True)
    payload: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    content: str = Field(default="")
    confidence: float = Field(default=1.0)
    importance_score: float = Field(default=0.0)
    source_type: MemorySourceType = Field(default=MemorySourceType.manual, index=True)
    source_session_id: str | None = Field(default=None, index=True)
    source_message_id: str | None = Field(default=None, index=True)
    source_execution_id: str | None = Field(default=None, index=True)
    version: int = Field(default=1)
    access_count: int = Field(default=0)
    last_accessed_at: datetime | None = Field(default=None, index=True)
    expires_at: datetime | None = Field(default=None, index=True)
    deleted_at: datetime | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def touch_updated_at(self, at: datetime | None = None) -> None:
        self.updated_at = utcnow() if at is None else at

    def touch_accessed_at(self, at: datetime | None = None) -> None:
        now = utcnow() if at is None else at
        self.last_accessed_at = now
        self.access_count += 1


@event.listens_for(MemoryRecord, "before_update")
def _touch_memory_updated_at(_mapper: Any, _connection: Any, target: MemoryRecord) -> None:
    target.touch_updated_at()
