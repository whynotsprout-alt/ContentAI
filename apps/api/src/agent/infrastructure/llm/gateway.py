from __future__ import annotations

from typing import Any

from agent.context.window import TokenCounter
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
        return self.settings.llm.chat_model

    def build_agent_model(self, *, tools: list[Any] | None = None) -> Any:
        return self.client.build_chat_model(
            model=self.settings.llm.chat_model,
            temperature=self.settings.llm.temperature,
            max_tokens=self.settings.llm.chat_max_tokens,
            tools=tools or [],
        )

    def build_hotspot_filter_model(self) -> Any:
        """Build the isolated structured model used only for hotspot scoring."""
        from agent.tools.hotspot_filter import HotspotFilterResult

        return self.client.build_structured_output_model(
            model=self.settings.llm.summary_model,
            temperature=0,
            max_tokens=self.settings.llm.structured_max_tokens,
            schema=HotspotFilterResult,
        )

    def build_token_counter(self, *, tools: list[Any] | None = None) -> TokenCounter:
        model = self.client.build_chat_model(
            model=self.settings.llm.chat_model,
            temperature=0,
            max_tokens=1,
            timeout_seconds=5.0,
            max_retries=0,
        )
        provider_count = getattr(model, "get_num_tokens_from_messages", None)
        if not callable(provider_count):
            return TokenCounter()
        bound_tools = list(tools or [])
        return TokenCounter(
            provider_count=lambda messages: provider_count(messages, tools=bound_tools),
            tools=bound_tools,
        )

    def build_structured_output_model(
        self,
        schema: type[Any],
        *,
        timeout_seconds: float = 240.0,
        max_retries: int = 2,
    ) -> Any:
        return self.client.build_structured_output_model(
            model=self.settings.llm.summary_model,
            temperature=0,
            max_tokens=self.settings.llm.structured_max_tokens,
            schema=schema,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )


model_gateway = ModelGateway()
