from __future__ import annotations

from pydantic import BaseModel


class AgentSettings(BaseModel):
    memory_after_turn_enabled: bool = True
    context_max_messages: int = 24
    context_min_focused_messages: int = 6
    max_iterations: int = 8
    recursion_limit: int = 20
    event_flush_interval_ms: int = 250
    event_flush_max_chars: int = 1200
    checkpoint_backend: str = "postgres"
    memory_backend: str = "postgres"
    tool_timeout_seconds: float = 60.0
    streaming_enabled: bool = True
