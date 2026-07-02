from __future__ import annotations

from typing import Any

from core.config import Settings
from langchain_openai import ChatOpenAI
from pydantic import SecretStr


def secret_value(value: str | SecretStr) -> str:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value or ""


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
        chat_model = ChatOpenAI(
            model=model,
            api_key=secret_value(self.settings.traffic_relay_api_key),
            base_url=self.settings.traffic_relay_base_url.rstrip("/"),
            temperature=temperature,
            max_completion_tokens=max_tokens,
            timeout=240.0,
            max_retries=2,
        )
        if not tools:
            return chat_model
        try:
            return chat_model.bind_tools(tools, tool_choice="auto")
        except TypeError:
            return chat_model.bind_tools(tools)
