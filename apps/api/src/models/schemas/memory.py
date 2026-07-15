from datetime import datetime
from typing import Any

from models.enums import MemoryOwnerType, MemorySourceType
from models.schemas.base import SchemaBase
from pydantic import Field


class MemoryItem(SchemaBase):
    key: str
    kind: str = "semantic"
    owner_type: MemoryOwnerType
    content: str
    payload: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 1.0
    importance_score: float = 0.0
    source_type: MemorySourceType = MemorySourceType.manual
    updated_at: datetime | None = None


class ConversationMemory(SchemaBase):
    short_term_summary: str = ""
    long_term_memories: list[MemoryItem] = Field(default_factory=list)
