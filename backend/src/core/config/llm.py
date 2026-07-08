from __future__ import annotations

from pydantic import BaseModel


class LLMSettings(BaseModel):
    chat_model: str = "claude-opus-4-6"
    planning_model: str = "claude-opus-4-6"
    summary_model: str = "claude-opus-4-6"
    embedding_model: str = ""
    temperature: float = 0.2
    chat_max_tokens: int = 4096
    structured_max_tokens: int = 4096
