from __future__ import annotations

from typing import Self

from pydantic import BaseModel, model_validator

DEFAULT_CONTEXT_WINDOW_TOKENS = 32_000
DEFAULT_MAX_OUTPUT_TOKENS = 8_000


class LLMSettings(BaseModel):
    temperature: float = 0.2
    context_window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS
    chat_max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    structured_max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS

    @model_validator(mode="after")
    def validate_runtime_budgets(self) -> Self:
        if not 0 <= self.temperature <= 2:
            raise ValueError("CONTENTAI_LLM__TEMPERATURE must be between 0 and 2.")
        if self.context_window_tokens < 1:
            raise ValueError("CONTENTAI_LLM__CONTEXT_WINDOW_TOKENS must be greater than 0.")
        if self.chat_max_tokens < 1:
            raise ValueError("CONTENTAI_LLM__CHAT_MAX_TOKENS must be greater than 0.")
        if self.structured_max_tokens < 1:
            raise ValueError("CONTENTAI_LLM__STRUCTURED_MAX_TOKENS must be greater than 0.")
        if self.chat_max_tokens >= self.context_window_tokens:
            raise ValueError(
                "CONTENTAI_LLM__CHAT_MAX_TOKENS must be less than "
                "CONTENTAI_LLM__CONTEXT_WINDOW_TOKENS."
            )
        if self.structured_max_tokens >= self.context_window_tokens:
            raise ValueError(
                "CONTENTAI_LLM__STRUCTURED_MAX_TOKENS must be less than "
                "CONTENTAI_LLM__CONTEXT_WINDOW_TOKENS."
            )
        return self
