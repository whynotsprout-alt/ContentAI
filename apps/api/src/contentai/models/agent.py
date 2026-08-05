from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, Index, UniqueConstraint, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from contentai.models.base import new_id, utcnow


class AgentProfile(SQLModel, table=True):
    __tablename__ = "agentprofile"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="ux_agentprofile_user_name"),
        Index("ux_agentprofile_id_user", "id", "user_id", unique=True),
        Index("ix_agentprofile_user_created_id", "user_id", "created_at", "id"),
        Index("ix_agentprofile_user_updated", "user_id", "updated_at"),
    )

    id: str = Field(default_factory=lambda: new_id("agt"), primary_key=True)
    user_id: str = Field(index=True, foreign_key="appuser.id", ondelete="CASCADE")
    name: str = Field(index=True)
    description: str = ""
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))

    def touch_updated_at(self, at: datetime | None = None) -> None:
        self.updated_at = utcnow() if at is None else at


class AgentVersion(SQLModel, table=True):
    __tablename__ = "agentversion"
    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="ux_agentversion_agent_version"),
        Index("ux_agentversion_id_agent", "id", "agent_id", unique=True),
        Index("ix_agentversion_agent_created", "agent_id", "created_at"),
    )

    id: str = Field(default_factory=lambda: new_id("agv"), primary_key=True)
    agent_id: str = Field(
        index=True,
        foreign_key="agentprofile.id",
        ondelete="CASCADE",
    )
    version: int = Field(default=1, index=True)
    topic_scoring_prompt: str = Field(default="")
    content_prompt: str
    hotspot_sources: list[str] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False),
    )
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))


@event.listens_for(AgentProfile, "before_update")
def _touch_agent_profile_updated_at(
    _mapper: object,
    _connection: object,
    target: AgentProfile,
) -> None:
    target.touch_updated_at()
