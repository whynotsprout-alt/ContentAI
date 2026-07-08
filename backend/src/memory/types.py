from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class MemoryEntry:
    key: str
    content: str
    kind: str = "semantic"
    payload: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime | None = None
    tenant_id: str = "local"
    user_id: str = "local-user"
    account_id: str | None = None
    session_id: str | None = None
    memory_scope: str = "long_term"
    confidence: float = 1.0
    importance_score: float = 0.0
    source_type: str = ""
    source_session_id: str | None = None
    source_message_id: str | None = None
    source_execution_id: str | None = None
    expires_at: datetime | None = None
    access_count: int = 0
    last_accessed_at: datetime | None = None
