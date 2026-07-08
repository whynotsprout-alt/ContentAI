from datetime import datetime
from typing import Any

from models.schemas.base import SchemaBase
from pydantic import Field


class MemoryItem(SchemaBase):
    key: str
    kind: str = "semantic"
    content: str
    payload: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0
    importance_score: float = 0.0
    source_type: str = ""
    updated_at: datetime | None = None


class ConversationMemory(SchemaBase):
    short_term_summary: str = ""
    long_term_memories: list[MemoryItem] = Field(default_factory=list)
