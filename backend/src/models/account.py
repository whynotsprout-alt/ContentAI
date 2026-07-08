from datetime import datetime

from core.hotspot_sources import DEFAULT_HOTSPOT_SOURCES
from models.base import new_id, utcnow
from sqlalchemy import Column, UniqueConstraint, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


class Account(SQLModel, table=True):
    __tablename__ = "account"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "name",
            name="ux_account_tenant_name",
        ),
    )

    id: str = Field(default_factory=lambda: new_id("acc"), primary_key=True)
    tenant_id: str = Field(default="local", index=True)
    name: str = Field(index=True)
    positioning: str
    topic_scoring_prompt: str
    content_creation_prompt: str
    hotspot_sources: list[str] = Field(
        default_factory=lambda: list(DEFAULT_HOTSPOT_SOURCES),
        sa_column=Column(JSONB, nullable=False),
    )
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def touch_updated_at(self, at: datetime | None = None) -> None:
        self.updated_at = utcnow() if at is None else at


@event.listens_for(Account, "before_update")
def _touch_account_updated_at(_mapper: object, _connection: object, target: Account) -> None:
    target.touch_updated_at()
