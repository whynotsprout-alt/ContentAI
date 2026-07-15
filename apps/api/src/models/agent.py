from __future__ import annotations

from datetime import datetime
from typing import Any

from models.base import new_id, utcnow
from models.enums import AgentStatus, AgentType
from sqlalchemy import Column, Index, UniqueConstraint, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class AgentProfile(SQLModel, table=True):
    __tablename__ = "agentprofile"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "owner_user_id",
            "name",
            name="ux_agentprofile_tenant_user_name",
        ),
        Index("ix_agentprofile_tenant_user", "tenant_id", "owner_user_id"),
        Index("ix_agentprofile_tenant_user_status", "tenant_id", "owner_user_id", "status"),
        Index("ix_agentprofile_tenant_user_type", "tenant_id", "owner_user_id", "agent_type"),
    )

    id: str = Field(default_factory=lambda: new_id("agt"), primary_key=True)
    tenant_id: str = Field(default="local", index=True)
    owner_user_id: str | None = Field(default=None, index=True)
    name: str = Field(index=True)
    description: str = ""
    agent_type: AgentType = Field(default=AgentType.custom, index=True)
    status: AgentStatus = Field(default=AgentStatus.draft, index=True)
    created_by_user_id: str | None = Field(default=None, index=True)
    updated_by_user_id: str | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def touch_updated_at(self, at: datetime | None = None) -> None:
        self.updated_at = utcnow() if at is None else at


class AgentVersion(SQLModel, table=True):
    __tablename__ = "agentversion"
    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="ux_agentversion_agent_version"),
        Index("ix_agentversion_agent_created", "agent_id", "created_at"),
    )

    id: str = Field(default_factory=lambda: new_id("agv"), primary_key=True)
    agent_id: str = Field(index=True, foreign_key="agentprofile.id")
    version: int = Field(default=1, index=True)
    topic_scoring_prompt: str = Field(default="")
    content_prompt: str
    graph_name: str = Field(default="default", index=True)
    tools_config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    memory_config: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    created_by_user_id: str | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow)


@event.listens_for(AgentProfile, "before_update")
def _touch_agent_profile_updated_at(
    _mapper: object,
    _connection: object,
    target: AgentProfile,
) -> None:
    target.touch_updated_at()
