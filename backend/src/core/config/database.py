from __future__ import annotations

from pydantic import BaseModel


class DatabaseSettings(BaseModel):
    url: str | None = None
    pool_size: int = 20
    max_overflow: int = 40
    pool_timeout: float = 30.0
    pool_recycle_seconds: int = 600
