from __future__ import annotations

from pydantic import BaseModel


class LoggingSettings(BaseModel):
    level: str = "INFO"
