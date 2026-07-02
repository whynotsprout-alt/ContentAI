from datetime import datetime

from models.base import new_id, utcnow
from sqlmodel import Field, SQLModel


class Account(SQLModel, table=True):
    __tablename__ = "account"

    id: str = Field(default_factory=lambda: new_id("acc"), primary_key=True)
    name: str = Field(index=True)
    description: str = ""
    instructions: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def touch_updated_at(self, at: datetime | None = None) -> None:
        self.updated_at = utcnow() if at is None else at
