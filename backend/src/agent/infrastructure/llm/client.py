from __future__ import annotations

from typing import Any

import anthropic
from core.config import Settings
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessageChunk
from pydantic import SecretStr


def secret_value(value: str | SecretStr) -> str:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value or ""


def anthropic_relay_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    return normalized if normalized.endswith("/anthropic") else f"{normalized}/anthropic"


class _ModelDumpDict(dict[str, Any]):
    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return _plain_json_dict(self)


class RelayCompatibleChatAnthropic(ChatAnthropic):
    def _make_message_chunk_from_anthropic_event(
        self,
        event: anthropic.types.RawMessageStreamEvent,
        *,
        stream_usage: bool = True,
        coerce_content_to_string: bool,
        block_start_event: anthropic.types.RawMessageStreamEvent | None = None,
    ) -> tuple[AIMessageChunk | None, anthropic.types.RawMessageStreamEvent | None]:
        _normalize_relay_event(event)
        return super()._make_message_chunk_from_anthropic_event(
            event,
            stream_usage=stream_usage,
            coerce_content_to_string=coerce_content_to_string,
            block_start_event=block_start_event,
        )


def _normalize_relay_event(event: Any) -> None:
    if getattr(event, "type", None) != "message_delta":
        return

    context_management = getattr(event, "context_management", None)
    if isinstance(context_management, dict) and not hasattr(context_management, "model_dump"):
        _safe_setattr(event, "context_management", _ModelDumpDict(context_management))

    delta = getattr(event, "delta", None)
    container = getattr(delta, "container", None) if delta is not None else None
    if isinstance(container, dict) and not hasattr(container, "model_dump"):
        _safe_setattr(delta, "container", _ModelDumpDict(container))


def _safe_setattr(target: Any, name: str, value: Any) -> None:
    try:
        setattr(target, name, value)
    except Exception:  # noqa: BLE001
        object.__setattr__(target, name, value)


def _plain_json_dict(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _plain_json_value(item)
        for key, item in value.items()
        if item is not None
    }


def _plain_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _plain_json_dict(value)
    if isinstance(value, list):
        return [_plain_json_value(item) for item in value]
    return value


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
        api_key = secret_value(self.settings.search.traffic_relay_api_key)
        has_tools = bool(tools)
        chat_model = RelayCompatibleChatAnthropic(
            model_name=model,
            api_key=api_key,
            base_url=anthropic_relay_base_url(self.settings.search.traffic_relay_base_url),
            default_headers={"Authorization": f"Bearer {api_key}"},
            temperature=temperature,
            max_tokens_to_sample=max_tokens,
            timeout=240.0,
            max_retries=2,
            disable_streaming=has_tools,
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
