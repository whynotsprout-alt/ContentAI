from __future__ import annotations

from pydantic import BaseModel


class LoggingSettings(BaseModel):
    level: str = "INFO"
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 7
