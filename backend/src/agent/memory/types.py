from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class MemoryEntry:
    key: str
    namespace: tuple[str, ...]
    content: str
    kind: str = "semantic"
    payload: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime | None = None
