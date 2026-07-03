from datetime import datetime

from core.hotspot_sources import DEFAULT_HOTSPOT_SOURCES
from models.base import new_id, utcnow
from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


class Account(SQLModel, table=True):
    __tablename__ = "account"

    id: str = Field(default_factory=lambda: new_id("acc"), primary_key=True)
    name: str = Field(index=True)
    positioning: str
    topic_scoring_prompt: str
    content_creation_prompt: str
    hotspot_sources: list[str] = Field(
        default_factory=lambda: list(DEFAULT_HOTSPOT_SOURCES),
        sa_column=Column(JSON, nullable=False),
    )
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def touch_updated_at(self, at: datetime | None = None) -> None:
        self.updated_at = utcnow() if at is None else at
