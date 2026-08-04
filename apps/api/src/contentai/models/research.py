from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Column, DateTime, ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from contentai.models.base import new_id, utcnow


class ResearchPackage(SQLModel, table=True):
    __tablename__ = "researchpackage"
    __table_args__ = (
        UniqueConstraint(
            "execution_id",
            "topic_hash",
            name="ux_researchpackage_execution_topic",
        ),
        ForeignKeyConstraint(
            ["execution_id", "session_id", "agent_version_id"],
            [
                "agentexecution.id",
                "agentexecution.session_id",
                "agentexecution.agent_version_id",
            ],
            name="fk_researchpackage_execution_lineage",
            ondelete="CASCADE",
        ),
        Index("ix_researchpackage_session_created", "session_id", "created_at"),
    )

    id: str = Field(default_factory=lambda: new_id("rsp"), primary_key=True)
    session_id: str = Field(
        index=True,
        foreign_key="chatsession.id",
        ondelete="CASCADE",
    )
    execution_id: str = Field(
        index=True,
        foreign_key="agentexecution.id",
        ondelete="CASCADE",
    )
    agent_version_id: str = Field(index=True, foreign_key="agentversion.id")
    topic: str
    topic_hash: str = Field(index=True)
    package_data: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    sources: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(JSONB, nullable=False),
    )
    provider_diagnostics: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSONB, nullable=False),
    )
    rendered_content: str
    valid_source_count: int = 0
    isolated_source_count: int = 0
    removed_unknown_reference_count: int = 0
    created_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))
    updated_at: datetime = Field(default_factory=utcnow, sa_type=DateTime(timezone=True))

    def touch_updated_at(self, at: datetime | None = None) -> None:
        self.updated_at = utcnow() if at is None else at
