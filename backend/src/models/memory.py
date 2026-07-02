from datetime import datetime

from models.base import json_dumps, new_id, utcnow
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class MemoryRecord(SQLModel, table=True):
    __tablename__ = "memoryrecord"

    __table_args__ = (
        UniqueConstraint(
            "namespace",
            "memory_key",
            name="ux_memoryrecord_namespace_key",
        ),
    )

    id: str = Field(default_factory=lambda: new_id("mem"), primary_key=True)
    namespace: str = Field(index=True)
    memory_key: str = Field(index=True)
    kind: str = Field(default="semantic", index=True)
    payload: str = Field(default_factory=lambda: json_dumps({}))
    content: str = Field(default="", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def touch_updated_at(self, at: datetime | None = None) -> None:
        self.updated_at = utcnow() if at is None else at
