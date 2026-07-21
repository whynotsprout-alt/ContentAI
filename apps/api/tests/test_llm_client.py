from __future__ import annotations

import json
import math
from typing import Any

import httpx
import pytest
from agent.infrastructure.llm.client import LangChainChatClient
from agent.infrastructure.llm.gateway import ModelGateway
from agent.runtime.errors import (
    MODEL_STREAM_INTERRUPTED_CODE,
    MODEL_STREAM_INTERRUPTED_MESSAGE,
    classify_runtime_error,
)
from core.config import Settings
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from pydantic import SecretStr


def _settings() -> Settings:
    return Settings(
        database={
            "url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test"
        }
    )


def _gateway(*, client: Any | None = None) -> ModelGateway:
    return ModelGateway(
        settings=_settings(),
        model_config_id="model-config-v7",
        base_url="https://models.example.test/custom-root",
        api_key=SecretStr("runtime-secret-key"),
        model_name="selected-model-v7",
        client=client,
    )


def test_openai_client_uses_exact_selected_root_model_and_safe_secret(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            observed.update(kwargs)
            self.disable_streaming = kwargs["disable_streaming"]

    monkeypatch.setattr("agent.infrastructure.llm.client.ChatOpenAI", FakeChatOpenAI)
    client = LangChainChatClient(
        base_url="https://models.example.test/custom-root",
        api_key=SecretStr("runtime-secret-key"),
        model_name="selected-model-v7",
    )

    model = client.build_chat_model(
        temperature=0.37,
        max_tokens=321,
        disable_streaming=True,
    )

    assert model.disable_streaming is True
    assert observed["base_url"] == "https://models.example.test/custom-root"
    assert observed["model"] == "selected-model-v7"
    assert observed["api_key"].get_secret_value() == "runtime-secret-key"
    assert observed["temperature"] == 0.37
    assert observed["max_tokens"] == 321
    assert "default_headers" not in observed
    assert "Authorization" not in repr(observed)
    assert "runtime-secret-key" not in repr(observed)


def test_agent_model_keeps_provider_streaming_when_tools_are_bound():
    @tool
    def sample_tool() -> str:
        """A sample tool."""
        return "ok"

    client = LangChainChatClient(
        base_url="https://models.example.test/v1",
        api_key=SecretStr("test-key"),
        model_name="selected-model",
    )

    plain_model = client.build_chat_model(temperature=0, max_tokens=128)
    tool_model = client.build_chat_model(
        temperature=0,
        max_tokens=128,
        tools=[sample_tool],
    )
    final_model = client.build_chat_model(
        temperature=0,
        max_tokens=128,
        disable_streaming=True,
    )

    assert plain_model.disable_streaming is False
    assert tool_model.bound.disable_streaming is False
    assert final_model.disable_streaming is True


def test_every_gateway_scenario_uses_execution_selected_model_and_tuning():
    observed: list[dict[str, Any]] = []

    class Client:
        def build_chat_model(self, **kwargs: Any) -> Any:
            observed.append(kwargs)
            return object()

        def build_structured_output_model(self, **kwargs: Any) -> Any:
            observed.append(kwargs)
            return object()

    gateway = _gateway(client=Client())
    gateway.build_agent_model()
    gateway.build_hotspot_filter_model()
    gateway.build_research_final_model()
    gateway.build_structured_output_model(dict)
    gateway.build_token_counter()

    assert len(observed) == 5
    assert {call["model"] for call in observed} == {"selected-model-v7"}
    assert observed[0]["temperature"] == _settings().llm.temperature
    assert observed[0]["max_tokens"] == _settings().llm.chat_max_tokens
    assert all(call["max_tokens"] > 0 for call in observed)


def test_research_final_gateway_builds_selection_schema_with_streaming_disabled():
    observed: dict[str, object] = {}

    class Client:
        def build_structured_output_model(self, **kwargs: Any):
            observed.update(kwargs)
            return "selection-model"

    gateway = _gateway(client=Client())

    assert gateway.build_research_final_model() == "selection-model"
    assert observed["schema"].__name__ == "ResearchFinalSelection"
    assert observed["disable_streaming"] is True


def test_model_gateway_combines_provider_message_count_with_local_tool_schemas():
    observed: list[list[object]] = []

    class ProviderModel:
        def get_num_tokens_from_messages(self, messages, *, tools=None):
            assert tools is None
            observed.append(messages)
            return 11

    class Client:
        def build_chat_model(self, **_kwargs):
            return ProviderModel()

    tool_schema = {
        "name": "search",
        "description": "Search for a source.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    }
    tools = [tool_schema]
    counter = _gateway(client=Client()).build_token_counter(tools=tools)
    messages = [HumanMessage(content="count me")]

    encoded_schema = json.dumps(
        tools,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    expected_tool_tokens = math.ceil(len(encoded_schema) / 4)
    assert counter.count_messages(messages) == 11 + expected_tool_tokens
    assert observed == [messages]


def test_gateway_fallback_token_counter_includes_bound_tool_schemas():
    class Client:
        def build_chat_model(self, **_kwargs):
            return object()

    message = [HumanMessage(content="count me")]
    tool_schema = {
        "name": "large_tool",
        "description": "schema payload " * 100,
        "parameters": {"type": "object", "properties": {"value": {"type": "string"}}},
    }

    without_tools = _gateway(client=Client()).build_token_counter()
    with_tools = _gateway(client=Client()).build_token_counter(tools=[tool_schema])

    assert with_tools.count_messages(message) > without_tools.count_messages(message)


def test_legacy_model_environment_names_are_not_runtime_configuration_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTENTAI_LLM__CHAT_MODEL", "legacy-chat")
    monkeypatch.setenv("CONTENTAI_LLM__PLANNING_MODEL", "legacy-planning")
    monkeypatch.setenv("CONTENTAI_LLM__SUMMARY_MODEL", "legacy-summary")

    settings = _settings()

    assert "chat_model" not in settings.llm.model_dump()
    assert "planning_model" not in settings.llm.model_dump()
    assert "summary_model" not in settings.llm.model_dump()
    assert "embedding_model" not in settings.llm.model_dump()


def test_remote_model_error_body_and_secrets_are_not_public() -> None:
    secret = "sk-remote-secret"
    remote_body = "provider diagnostic body that must stay private"
    error = RuntimeError(f"401 Authorization: Bearer {secret}; body={remote_body}")

    detail = classify_runtime_error(error)

    assert secret not in detail.message
    assert remote_body not in detail.message
    assert "Authorization" not in detail.message


@pytest.mark.parametrize(
    "error",
    [
        httpx.RemoteProtocolError("incomplete chunked read"),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("connection refused"),
    ],
)
def test_model_stream_transport_errors_are_retryable(error: Exception):
    detail = classify_runtime_error(error)

    assert detail.message == MODEL_STREAM_INTERRUPTED_MESSAGE
    assert detail.code == MODEL_STREAM_INTERRUPTED_CODE
    assert detail.retryable is True
