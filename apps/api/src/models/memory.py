from __future__ import annotations

from datetime import datetime
from typing import Any

from models.base import new_id, utcnow
from models.enums import MemoryKind, MemoryOwnerType, MemoryScope, MemorySourceType
from sqlalchemy import CheckConstraint, Column, Index, event, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class MemoryRecord(SQLModel, table=True):
    __tablename__ = "memoryrecord"

    __table_args__ = (
        Index(
            "ix_memoryrecord_lookup",
            "tenant_id",
            "user_id",
            "owner_type",
            "agent_id",
            "memory_scope",
            "kind",
            "updated_at",
        ),
        Index(
            "ux_memoryrecord_active_user_long_term",
            "tenant_id",
            "user_id",
            "memory_key",
            unique=True,
            postgresql_where=text(
                "memory_scope = 'long_term' AND owner_type = 'user' AND deleted_at IS NULL"
            ),
        ),
        Index(
            "ux_memoryrecord_active_agent_long_term",
            "tenant_id",
            "user_id",
            "agent_id",
            "memory_key",
            unique=True,
            postgresql_where=text(
                "memory_scope = 'long_term' AND owner_type = 'agent' AND deleted_at IS NULL"
            ),
        ),
        Index(
            "ux_memoryrecord_active_session",
            "tenant_id",
            "user_id",
            "session_id",
            "memory_key",
            unique=True,
            postgresql_where=text("owner_type = 'session' AND deleted_at IS NULL"),
        ),
        CheckConstraint(
            "owner_type IN ('user', 'agent', 'session')",
            name="ck_memoryrecord_owner_type",
        ),
        CheckConstraint(
            "source_type IN "
            "('manual', 'user_message', 'turn_summary', 'summary', 'tool', 'system')",
            name="ck_memoryrecord_source_type",
        ),
    )

    id: str = Field(default_factory=lambda: new_id("mem"), primary_key=True)
    tenant_id: str = Field(index=True)
    user_id: str = Field(index=True)
    owner_type: MemoryOwnerType = Field(index=True)
    agent_id: str | None = Field(default=None, index=True)
    session_id: str | None = Field(default=None, index=True)
    memory_scope: MemoryScope = Field(default=MemoryScope.long_term, index=True)
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
