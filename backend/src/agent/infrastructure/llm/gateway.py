from __future__ import annotations

from typing import Any

from agent.infrastructure.llm.client import LangChainChatClient
from core.config import Settings, get_settings


class ModelGateway:
    """Single lower-level model gateway for the LangGraph agent."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: LangChainChatClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client or LangChainChatClient(self.settings)

    @property
    def model_name(self) -> str:
        return self.settings.llm_model

    def build_agent_model(self, *, tools: list[Any] | None = None) -> Any:
        return self.client.build_chat_model(
            model=self.settings.llm_model,
            temperature=self.settings.llm_temperature,
            max_tokens=self.settings.llm_max_tokens,
            tools=tools or [],
        )


model_gateway = ModelGateway()
