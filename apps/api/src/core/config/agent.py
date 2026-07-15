from __future__ import annotations

from pydantic import BaseModel


class AgentSettings(BaseModel):
    memory_after_turn_enabled: bool = True
    context_max_messages: int = 40
    context_min_focused_messages: int = 6
    max_iterations: int = 8
    recursion_limit: int = 20
    event_flush_interval_ms: int = 250
    event_flush_max_chars: int = 1200
    runtime_cache_capacity: int = 32
    checkpoint_backend: str = "postgres"
    memory_backend: str = "postgres"
    tool_timeout_seconds: float = 60.0
    celery_queue: str = "agent-executions"
    celery_background_queue: str = "agent-background"
    worker_concurrency: int = 4
    worker_claim_timeout_seconds: int = 120
    worker_lease_seconds: int = 120
    worker_heartbeat_seconds: int = 15
    max_execution_attempts: int = 3
    postprocess_max_attempts: int = 3
    postprocess_retry_base_seconds: int = 5
    user_daily_run_limit: int = 500
    celery_visibility_timeout_seconds: int = 7200
    worker_soft_time_limit_seconds: int = 1800
    worker_time_limit_seconds: int = 1860
    event_replay_poll_seconds: float = 0.25
