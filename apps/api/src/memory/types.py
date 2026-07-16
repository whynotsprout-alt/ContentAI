from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from models.enums import MemorySourceType


@dataclass(frozen=True)
class MemoryEntry:
    key: str
    content: str
    user_id: str
    kind: str = "semantic"
    payload: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime | None = None
    agent_id: str | None = None
    session_id: str | None = None
    confidence: float = 1.0
    importance_score: float = 0.0
    source_type: MemorySourceType = MemorySourceType.manual
    source_session_id: str | None = None
    source_message_id: str | None = None
    source_execution_id: str | None = None
    expires_at: datetime | None = None
    access_count: int = 0
    last_accessed_at: datetime | None = None

    @property
    def memory_scope(self) -> str:
        return "long_term" if self.agent_id is not None else "short_term"
