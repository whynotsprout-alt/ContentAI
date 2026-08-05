from __future__ import annotations

from pydantic import BaseModel, Field


class ServerSettings(BaseModel):
    app_name: str = "ContentAI"
    frontend_origins: str | list[str] = Field(default_factory=list)
    trusted_proxy_cidrs: str | list[str] = Field(default_factory=list)
    outbox_readiness_threshold: int = 10_000
    outbox_max_age_seconds: int = 30
