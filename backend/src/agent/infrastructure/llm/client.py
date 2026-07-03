from __future__ import annotations

from typing import Any

from core.config import Settings
from langchain_anthropic import ChatAnthropic
from pydantic import SecretStr


def secret_value(value: str | SecretStr) -> str:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value or ""


def anthropic_relay_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    return normalized if normalized.endswith("/anthropic") else f"{normalized}/anthropic"


class LangChainChatClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def build_chat_model(
        self,
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        tools: list[Any] | None = None,
    ) -> Any:
        api_key = secret_value(self.settings.traffic_relay_api_key)
        chat_model = ChatAnthropic(
            model_name=model,
            api_key=api_key,
            base_url=anthropic_relay_base_url(self.settings.traffic_relay_base_url),
            default_headers={"Authorization": f"Bearer {api_key}"},
            temperature=temperature,
            max_tokens_to_sample=max_tokens,
            timeout=240.0,
            max_retries=2,
            disable_streaming=True,
        )
        if not tools:
            return chat_model
        try:
            return chat_model.bind_tools(tools, tool_choice="auto")
        except TypeError:
            return chat_model.bind_tools(tools)

    def build_structured_output_model(
        self,
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        schema: type[Any],
    ) -> Any:
        chat_model = self.build_chat_model(
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return chat_model.with_structured_output(schema)
