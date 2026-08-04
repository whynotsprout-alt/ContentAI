from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import httpx
import openai
import pytest
from contentai.agent.infrastructure.llm import client as client_module
from contentai.agent.infrastructure.llm.client import LangChainChatClient
from contentai.agent.infrastructure.llm.gateway import ModelGateway
from contentai.agent.runtime.errors import (
    MODEL_STREAM_INTERRUPTED_CODE,
    MODEL_STREAM_INTERRUPTED_MESSAGE,
    classify_runtime_error,
)
from contentai.core.config import Settings
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGenerationChunk
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

    monkeypatch.setattr(
        "contentai.agent.infrastructure.llm.client._UsageAwareChatOpenAI", FakeChatOpenAI
    )
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
    assert observed["stream_usage"] is True
    assert isinstance(observed["http_client"], httpx.Client)
    assert isinstance(observed["http_async_client"], httpx.AsyncClient)
    assert observed["http_client"].follow_redirects is False
    assert observed["http_async_client"].follow_redirects is False
    assert observed["http_client"]._trust_env is False
    assert observed["http_async_client"]._trust_env is False
    assert "default_headers" not in observed
    assert "Authorization" not in repr(observed)
    assert "runtime-secret-key" not in repr(observed)


def test_openai_client_omits_temperature_in_auto_mode(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            observed.update(kwargs)

    monkeypatch.setattr(
        "contentai.agent.infrastructure.llm.client._UsageAwareChatOpenAI", FakeChatOpenAI
    )
    client = LangChainChatClient(
        base_url="https://models.example.test/custom-root",
        api_key=SecretStr("runtime-secret-key"),
        model_name="selected-model-v7",
    )

    client.build_chat_model(temperature=None, max_tokens=128)

    assert "temperature" not in observed


def test_stream_usage_falls_back_once_when_endpoint_rejects_stream_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[bool | None] = []

    def fake_stream(
        _self: Any,
        *_args: Any,
        stream_usage: bool | None = None,
        **_kwargs: Any,
    ):
        attempts.append(stream_usage)
        if stream_usage:
            request = httpx.Request("POST", "https://models.example.test/v1/chat/completions")
            response = httpx.Response(400, request=request)
            raise openai.BadRequestError(
                "Unsupported parameter: stream_options",
                response=response,
                body={"error": {"message": "stream_options is not supported"}},
            )
        yield ChatGenerationChunk(message=AIMessageChunk(content="fallback"))

    monkeypatch.setattr(client_module.ChatOpenAI, "_stream", fake_stream)
    client = LangChainChatClient(
        base_url="https://models.example.test/v1",
        api_key=SecretStr("test-key"),
        model_name="selected-model",
    )
    try:
        model = client.build_chat_model(temperature=0, max_tokens=128)
        first = [chunk.message.content for chunk in model._stream([HumanMessage(content="ping")])]
        second = [chunk.message.content for chunk in model._stream([HumanMessage(content="again")])]
        assert first == ["fallback"]
        assert second == ["fallback"]
    finally:
        client.close()

    assert attempts == [True, False, False]


def test_async_stream_usage_falls_back_once_when_endpoint_rejects_stream_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[bool | None] = []

    async def fake_astream(
        _self: Any,
        *_args: Any,
        stream_usage: bool | None = None,
        **_kwargs: Any,
    ):
        attempts.append(stream_usage)
        if stream_usage:
            request = httpx.Request("POST", "https://models.example.test/v1/chat/completions")
            response = httpx.Response(422, request=request)
            raise openai.UnprocessableEntityError(
                "Unsupported parameter: include_usage",
                response=response,
                body={"error": {"message": "include_usage is not supported"}},
            )
        yield ChatGenerationChunk(message=AIMessageChunk(content="fallback"))

    async def collect(model: Any, content: str) -> list[str | list[str | dict[str, Any]]]:
        return [
            chunk.message.content
            async for chunk in model._astream([HumanMessage(content=content)])
        ]

    monkeypatch.setattr(client_module.ChatOpenAI, "_astream", fake_astream)
    client = LangChainChatClient(
        base_url="https://models.example.test/v1",
        api_key=SecretStr("test-key"),
        model_name="selected-model",
    )
    try:
        model = client.build_chat_model(temperature=0, max_tokens=128)
        first = asyncio.run(collect(model, "ping"))
        second = asyncio.run(collect(model, "again"))
    finally:
        client.close()

    assert first == ["fallback"]
    assert second == ["fallback"]
    assert attempts == [True, False, False]


def test_responses_api_sync_stream_does_not_receive_chat_completion_stream_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[dict[str, Any]] = []

    def fake_stream(_self: Any, *_args: Any, **kwargs: Any):
        observed.append(kwargs)
        yield ChatGenerationChunk(message=AIMessageChunk(content="response"))

    monkeypatch.setattr(client_module.ChatOpenAI, "_stream", fake_stream)
    client = LangChainChatClient(
        base_url="https://models.example.test/v1",
        api_key=SecretStr("test-key"),
        model_name="gpt-5.4-pro",
    )
    try:
        model = client.build_chat_model(temperature=0, max_tokens=128)
        chunks = [
            chunk.message.content
            for chunk in model._stream([HumanMessage(content="ping")])
        ]
    finally:
        client.close()

    assert chunks == ["response"]
    assert observed == [{}]


def test_responses_api_async_stream_does_not_receive_chat_completion_stream_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[dict[str, Any]] = []

    async def fake_astream(_self: Any, *_args: Any, **kwargs: Any):
        observed.append(kwargs)
        yield ChatGenerationChunk(message=AIMessageChunk(content="response"))

    async def collect(model: Any) -> list[str | list[str | dict[str, Any]]]:
        return [
            chunk.message.content
            async for chunk in model._astream([HumanMessage(content="ping")])
        ]

    monkeypatch.setattr(client_module.ChatOpenAI, "_astream", fake_astream)
    client = LangChainChatClient(
        base_url="https://models.example.test/v1",
        api_key=SecretStr("test-key"),
        model_name="gpt-5.4-pro",
    )
    try:
        model = client.build_chat_model(temperature=0, max_tokens=128)
        chunks = asyncio.run(collect(model))
    finally:
        client.close()

    assert chunks == ["response"]
    assert observed == [{}]


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


def test_concurrent_immediate_close_keeps_the_first_future_canonical_until_callback_transition(
    monkeypatch,
) -> None:
    client = client_module._OwnerLoopAsyncClient(
        transport=httpx.MockTransport(lambda _request: None)
    )
    callback_waiting = threading.Event()
    release_callback = threading.Event()
    submitted: list[Future[None]] = []

    class CallbackBarrierFuture(Future[None]):
        def add_done_callback(self, fn) -> None:
            callback_waiting.set()
            assert release_callback.wait(timeout=1.0)
            super().add_done_callback(fn)

    def submit_close(coroutine, _loop) -> Future[None]:
        coroutine.close()
        future: Future[None]
        if submitted:
            future = Future()
        else:
            future = CallbackBarrierFuture()
            future.set_result(None)
        submitted.append(future)
        return future

    monkeypatch.setattr(client_module.asyncio, "run_coroutine_threadsafe", submit_close)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            first_call = executor.submit(client._submit_close)
            assert callback_waiting.wait(timeout=1.0)
            second_future = client._submit_close()
            release_callback.set()
            first_future = first_call.result(timeout=1.0)

        assert second_future is first_future
        assert len(submitted) == 1
        assert client._owner_stopped.wait(timeout=1.0)
    finally:
        release_callback.set()
        for future in submitted:
            if not future.done():
                future.cancel()
        owner_loop = client._owner_loop
        if owner_loop is not None and not client._owner_stopped.is_set():
            owner_loop.call_soon_threadsafe(owner_loop.stop)
        client._owner_thread.join(timeout=1.0)


def test_failed_canonical_close_allows_one_shared_retry_after_callback_transition(
    monkeypatch,
) -> None:
    client = client_module._OwnerLoopAsyncClient(
        transport=httpx.MockTransport(lambda _request: None)
    )
    callback_waiting = threading.Event()
    release_callback = threading.Event()
    submitted: list[Future[None]] = []

    class CallbackBarrierFuture(Future[None]):
        def add_done_callback(self, fn) -> None:
            callback_waiting.set()
            assert release_callback.wait(timeout=1.0)
            super().add_done_callback(fn)

    def submit_close(coroutine, _loop) -> Future[None]:
        coroutine.close()
        future: Future[None]
        if submitted:
            future = Future()
        else:
            future = CallbackBarrierFuture()
            future.set_exception(RuntimeError("first close failed"))
        submitted.append(future)
        return future

    monkeypatch.setattr(client_module.asyncio, "run_coroutine_threadsafe", submit_close)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            first_call = executor.submit(client._submit_close)
            assert callback_waiting.wait(timeout=1.0)
            second_future = client._submit_close()
            release_callback.set()
            first_future = first_call.result(timeout=1.0)

        assert second_future is first_future
        with pytest.raises(RuntimeError, match="first close failed"):
            first_future.result()

        retry_future = client._submit_close()
        assert client._submit_close() is retry_future
        assert len(submitted) == 2
        retry_future.set_result(None)
        assert client._owner_stopped.wait(timeout=1.0)
    finally:
        release_callback.set()
        for future in submitted:
            if not future.done():
                future.cancel()
        owner_loop = client._owner_loop
        if owner_loop is not None and not client._owner_stopped.is_set():
            owner_loop.call_soon_threadsafe(owner_loop.stop)
        client._owner_thread.join(timeout=1.0)


@pytest.mark.parametrize("stale_by", ["identity", "generation"])
@pytest.mark.parametrize("succeeded", [True, False], ids=["success", "failure"])
def test_stale_close_callback_cannot_mutate_current_canonical_state(
    stale_by: str,
    succeeded: bool,
) -> None:
    client = client_module._OwnerLoopAsyncClient(
        transport=httpx.MockTransport(lambda _request: None)
    )
    canonical_future: Future[None] = Future()
    client._close_future = canonical_future
    client._close_generation = 2
    completed_future = Future() if stale_by == "identity" else canonical_future
    generation = 2 if stale_by == "identity" else 1
    if succeeded:
        completed_future.set_result(None)
    else:
        completed_future.set_exception(RuntimeError("stale close failed"))

    try:
        client._finish_close(completed_future, generation=generation)

        assert client._close_future is canonical_future
        assert client._async_closed is False
        assert not client._owner_stopped.wait(timeout=0.05)
    finally:
        if not canonical_future.done():
            canonical_future.cancel()
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
    assert all(call["temperature"] == 0.7 for call in observed[1:4])
    assert all(call["max_tokens"] == 6_000 for call in observed[1:4])
    assert observed[4]["temperature"] == 0.7
    assert observed[4]["max_tokens"] == 1


def test_auto_temperature_is_used_by_every_gateway_scenario():
    observed: list[dict[str, Any]] = []

    class Client:
        def build_chat_model(self, **kwargs: Any) -> Any:
            observed.append(kwargs)
            return object()

        def build_structured_output_model(self, **kwargs: Any) -> Any:
            observed.append(kwargs)
            return object()

    gateway = ModelGateway(
        model_config_id="model-config-auto",
        base_url="https://models.example.test/custom-root",
        api_key=SecretStr("runtime-secret-key"),
        model_name="selected-model-v7",
        temperature=None,
        context_window_tokens=200_000,
        chat_max_tokens=12_000,
        structured_max_tokens=6_000,
        client=Client(),
    )

    gateway.build_agent_model()
    gateway.build_hotspot_filter_model()
    gateway.build_research_final_model()
    gateway.build_structured_output_model(dict)
    gateway.build_token_counter()

    assert len(observed) == 5
    assert all(call["temperature"] is None for call in observed)


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


def test_model_gateway_reuses_token_counter_for_same_tool_instances() -> None:
    observed: list[dict[str, Any]] = []

    class ProviderModel:
        def get_num_tokens_from_messages(self, _messages):
            return 11

    class Client:
        def build_chat_model(self, **kwargs: Any) -> ProviderModel:
            observed.append(kwargs)
            return ProviderModel()

    tool_schema = {
        "name": "search",
        "description": "Search for a source.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    }
    gateway = _gateway(client=Client())

    first = gateway.build_token_counter(tools=[tool_schema])
    second = gateway.build_token_counter(tools=[tool_schema])

    assert second is first
    assert len(observed) == 1


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
