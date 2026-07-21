from __future__ import annotations

from typing import Any

from agent.context.window import TokenCounter
from agent.infrastructure.llm.client import LangChainChatClient
from core.config import Settings
from pydantic import SecretStr


class ModelGateway:
    """Models for one immutable execution-selected provider configuration."""

    def __init__(
        self,
        *,
        settings: Settings,
        model_config_id: str,
        base_url: str,
        api_key: SecretStr,
        model_name: str,
        client: LangChainChatClient | None = None,
    ) -> None:
        self.settings = settings
        self.model_config_id = model_config_id
        self.base_url = base_url
        self.model_name = model_name
        self.client = client or LangChainChatClient(
            base_url=base_url,
            api_key=api_key,
            model_name=model_name,
        )

    def build_agent_model(self, *, tools: list[Any] | None = None) -> Any:
        return self.client.build_chat_model(
            model=self.model_name,
            temperature=self.settings.llm.temperature,
            max_tokens=self.settings.llm.chat_max_tokens,
            tools=tools or [],
        )

    def build_hotspot_filter_model(self) -> Any:
        from agent.tools.hotspot_filter import HotspotFilterResult

        return self.client.build_structured_output_model(
            model=self.model_name,
            temperature=0,
            max_tokens=self.settings.llm.structured_max_tokens,
            schema=HotspotFilterResult,
        )

    def build_research_final_model(self) -> Any:
        from agent.workflows.final_evidence import ResearchFinalSelection

        return self.client.build_structured_output_model(
            model=self.model_name,
            temperature=0,
            max_tokens=self.settings.llm.structured_max_tokens,
            schema=ResearchFinalSelection,
            disable_streaming=True,
        )

    def build_token_counter(self, *, tools: list[Any] | None = None) -> TokenCounter:
        model = self.client.build_chat_model(
            model=self.model_name,
            temperature=0,
            max_tokens=1,
            timeout_seconds=5.0,
            max_retries=0,
        )
        provider_count = getattr(model, "get_num_tokens_from_messages", None)
        bound_tools = list(tools or [])
        if not callable(provider_count):
            return TokenCounter(tools=bound_tools)
        return TokenCounter(
            provider_count=lambda messages: provider_count(messages),
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
            model=self.model_name,
            temperature=0,
            max_tokens=self.settings.llm.structured_max_tokens,
            schema=schema,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )
