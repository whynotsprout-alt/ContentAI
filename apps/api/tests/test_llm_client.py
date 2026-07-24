from __future__ import annotations

import asyncio
import json
import math
import time
from concurrent.futures import Future
from typing import Any

import httpx
import pytest
from agent.infrastructure.llm import client as client_module
from agent.infrastructure.llm.client import LangChainChatClient
from agent.infrastructure.llm.gateway import ModelGateway
from agent.runtime.errors import (
    MODEL_STREAM_INTERRUPTED_CODE,
    MODEL_STREAM_INTERRUPTED_MESSAGE,
    classify_runtime_error,
)
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from pydantic import SecretStr


def _gateway(*, client: Any | None = None) -> ModelGateway:
    return ModelGateway(
        model_config_id="model-config-v7",
        base_url="https://models.example.test/custom-root",
        api_key=SecretStr("runtime-secret-key"),
        model_name="selected-model-v7",
        temperature=0.7,
        context_window_tokens=200_000,
        chat_max_tokens=12_000,
        structured_max_tokens=6_000,
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
    assert isinstance(observed["http_client"], httpx.Client)
    assert isinstance(observed["http_async_client"], httpx.AsyncClient)
    assert observed["http_client"].follow_redirects is False
    assert observed["http_async_client"].follow_redirects is False
    assert observed["http_client"]._trust_env is False
    assert observed["http_async_client"]._trust_env is False
    assert "default_headers" not in observed
    assert "Authorization" not in repr(observed)
    assert "runtime-secret-key" not in repr(observed)


def test_langchain_client_closes_owned_http_clients_once(monkeypatch) -> None:
    class SyncClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    class AsyncClient:
        def __init__(self, **_kwargs: Any) -> None:
            self.close_count = 0

        def close_from_sync(self) -> bool:
            self.close_count += 1
            return True

        async def aclose(self) -> None:
            self.close_count += 1

    monkeypatch.setattr(client_module.httpx, "Client", SyncClient)
    monkeypatch.setattr(client_module, "_OwnerLoopAsyncClient", AsyncClient)
    client = LangChainChatClient(
        base_url="https://models.example.test/v1",
        api_key=SecretStr("test-key"),
        model_name="selected-model",
    )

    client.close()
    client.close()

    assert client._http_client.close_count == 1
    assert client._http_async_client.close_count == 1


def test_async_http_client_closes_on_its_request_owner_loop(monkeypatch) -> None:
    class LoopBoundTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.request_loop = None
            self.close_loop = None

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.request_loop = asyncio.get_running_loop()
            return httpx.Response(200, json={"ok": True}, request=request)

        async def aclose(self) -> None:
            self.close_loop = asyncio.get_running_loop()
            if self.close_loop is not self.request_loop:
                raise RuntimeError("async transport closed outside its owner loop")

    transport = LoopBoundTransport()
    monkeypatch.setattr(client_module, "PinnedAsyncModelTransport", lambda **_kwargs: transport)
    client = LangChainChatClient(
        base_url="https://models.example.test/v1",
        api_key=SecretStr("test-key"),
        model_name="selected-model",
    )

    asyncio.run(client._http_async_client.get("https://models.example.test/v1/models"))
    client.close()

    assert transport.close_loop is transport.request_loop


def test_async_http_stream_stays_on_its_request_owner_loop(monkeypatch) -> None:
    class LoopBoundStream(httpx.AsyncByteStream):
        def __init__(self, owner_loop: asyncio.AbstractEventLoop) -> None:
            self.owner_loop = owner_loop
            self.iteration_loop = None
            self.close_loop = None

        async def __aiter__(self):
            self.iteration_loop = asyncio.get_running_loop()
            if self.iteration_loop is not self.owner_loop:
                raise RuntimeError("async stream consumed outside its owner loop")
            yield b"streamed"

        async def aclose(self) -> None:
            self.close_loop = asyncio.get_running_loop()
            if self.close_loop is not self.owner_loop:
                raise RuntimeError("async stream closed outside its owner loop")

    class StreamingTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.request_loop = None
            self.stream = None

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.request_loop = asyncio.get_running_loop()
            self.stream = LoopBoundStream(self.request_loop)
            return httpx.Response(200, stream=self.stream, request=request)

    transport = StreamingTransport()
    monkeypatch.setattr(client_module, "PinnedAsyncModelTransport", lambda **_kwargs: transport)
    client = LangChainChatClient(
        base_url="https://models.example.test/v1",
        api_key=SecretStr("test-key"),
        model_name="selected-model",
    )

    async def consume_stream() -> bytes:
        async with client._http_async_client.stream(
            "GET", "https://models.example.test/v1/chat/completions"
        ) as response:
            return b"".join([chunk async for chunk in response.aiter_bytes()])

    assert asyncio.run(consume_stream()) == b"streamed"
    client.close()
    assert transport.stream.iteration_loop is transport.request_loop
    assert transport.stream.close_loop is transport.request_loop


def test_immediate_async_close_completion_does_not_reenter_close_lock(monkeypatch) -> None:
    client = client_module._OwnerLoopAsyncClient(
        transport=httpx.MockTransport(lambda _request: None)
    )

    class ImmediateFuture(Future[None]):
        def add_done_callback(self, fn) -> None:
            assert not client._close_state_lock.locked()
            super().add_done_callback(fn)

    def complete_immediately(coroutine, _loop) -> Future[None]:
        coroutine.close()
        future: Future[None] = ImmediateFuture()
        future.set_result(None)
        return future

    monkeypatch.setattr(client_module.asyncio, "run_coroutine_threadsafe", complete_immediately)
    try:
        client._submit_close()
    finally:
        owner_loop = client._owner_loop
        if owner_loop is not None and not client._owner_stopped.is_set():
            owner_loop.call_soon_threadsafe(owner_loop.stop)
        client._owner_thread.join(timeout=1.0)


def test_async_http_client_close_failure_can_be_retried(monkeypatch) -> None:
    class FlakyCloseTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.close_attempts = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True}, request=request)

        async def aclose(self) -> None:
            self.close_attempts += 1
            if self.close_attempts == 1:
                raise RuntimeError("first close failed")

    transport = FlakyCloseTransport()
    monkeypatch.setattr(client_module, "PinnedAsyncModelTransport", lambda **_kwargs: transport)
    client = LangChainChatClient(
        base_url="https://models.example.test/v1",
        api_key=SecretStr("test-key"),
        model_name="selected-model",
    )

    with pytest.raises(RuntimeError, match="first close failed"):
        client.close()
    client.close()

    assert transport.close_attempts == 2


def test_close_called_from_running_loop_never_synchronously_joins(monkeypatch) -> None:
    class SlowCloseTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True}, request=request)

        async def aclose(self) -> None:
            await asyncio.sleep(0.2)

    monkeypatch.setattr(
        client_module,
        "PinnedAsyncModelTransport",
        lambda **_kwargs: SlowCloseTransport(),
    )
    client = LangChainChatClient(
        base_url="https://models.example.test/v1",
        api_key=SecretStr("test-key"),
        model_name="selected-model",
    )

    async def exercise() -> float:
        started_at = time.perf_counter()
        client.close()
        elapsed = time.perf_counter() - started_at
        await asyncio.sleep(0.25)
        return elapsed

    assert asyncio.run(exercise()) < 0.05


def test_model_gateway_close_delegates_once() -> None:
    class Client:
        def __init__(self) -> None:
            self.close_count = 0

        def close(self) -> None:
            self.close_count += 1

    client = Client()
    gateway = _gateway(client=client)

    gateway.close()
    gateway.close()

    assert client.close_count == 1


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
    assert observed[0]["temperature"] == 0.7
    assert observed[0]["max_tokens"] == 12_000
    assert all(call["temperature"] == 0 for call in observed[1:4])
    assert all(call["max_tokens"] == 6_000 for call in observed[1:4])
    assert observed[4]["temperature"] == 0
    assert observed[4]["max_tokens"] == 1


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
