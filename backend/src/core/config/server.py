from __future__ import annotations

from pydantic import BaseModel, Field


class ServerSettings(BaseModel):
    app_name: str = "ContentAI"
    frontend_origins: str | list[str] = Field(default_factory=list)
