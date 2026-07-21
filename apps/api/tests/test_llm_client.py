import httpx
import pytest
from agent.infrastructure.llm.client import LangChainChatClient, RelayCompatibleChatAnthropic
from agent.infrastructure.llm.gateway import ModelGateway
from agent.runtime.errors import (
    MODEL_STREAM_INTERRUPTED_CODE,
    MODEL_STREAM_INTERRUPTED_MESSAGE,
    classify_runtime_error,
)
from anthropic.types import MessageDeltaUsage, RawMessageDeltaEvent
from core.config import Settings
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool


def test_relay_context_management_dict_stream_event_is_supported():
    delta_type = RawMessageDeltaEvent.model_fields["delta"].annotation
    event = RawMessageDeltaEvent(
        type="message_delta",
        delta=delta_type(stop_reason="end_turn", stop_sequence=None),
        usage=MessageDeltaUsage(output_tokens=1),
        context_management={"mode": "relay"},
    )
    model = RelayCompatibleChatAnthropic(
        model_name="claude-test",
        api_key="test-key",
        base_url="https://example.test/v1/anthropic",
    )

    chunk, block_start_event = model._make_message_chunk_from_anthropic_event(
        event,
        stream_usage=True,
        coerce_content_to_string=True,
    )

    assert block_start_event is None
    assert chunk is not None
    assert chunk.response_metadata["context_management"] == {"mode": "relay"}


def test_agent_model_keeps_provider_streaming_when_tools_are_bound():
    @tool
    def sample_tool() -> str:
        """A sample tool."""
        return "ok"

    client = LangChainChatClient(
        Settings(
            database={
                "url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test"
            },
            search={"traffic_relay_api_key": "test-key"},
        )
    )

    plain_model = client.build_chat_model(
        model="claude-test",
        temperature=0,
        max_tokens=128,
    )
    tool_model = client.build_chat_model(
        model="claude-test",
        temperature=0,
        max_tokens=128,
        tools=[sample_tool],
    )

    assert plain_model.disable_streaming is False
    assert tool_model.bound.disable_streaming is False


def test_model_gateway_exposes_provider_token_counter_with_tools():
    observed: list[tuple[list[object], list[object]]] = []

    class ProviderModel:
        def get_num_tokens_from_messages(self, messages, *, tools=None):
            observed.append((messages, tools or []))
            return 11

    class Client:
        def build_chat_model(self, **_kwargs):
            return ProviderModel()

    settings = Settings(
        database={
            "url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test"
        },
        search={"traffic_relay_api_key": "test-key"},
    )
    tools = [object()]
    counter = ModelGateway(settings=settings, client=Client()).build_token_counter(tools=tools)
    messages = [HumanMessage(content="count me")]

    assert counter.count_messages(messages) == 11
    assert observed == [(messages, tools)]


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
