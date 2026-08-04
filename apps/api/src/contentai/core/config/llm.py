from __future__ import annotations

from pydantic import BaseModel

DEFAULT_CONTEXT_WINDOW_TOKENS = 32_000
DEFAULT_MAX_OUTPUT_TOKENS = 8_000


class LLMSettings(BaseModel):
    temperature: float = 0.2
    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS
    chat_max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    structured_max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
