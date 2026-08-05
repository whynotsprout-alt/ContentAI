from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
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
    is_retryable_model_stream_error,
    model_retry_delay_seconds,
)
from contentai.agent.runtime.model_invocation import (
    InvocationAttempt,
    ainvoke_model,
    invoke_model,
)
from contentai.core.config import Settings
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import tool
from model_config_helpers import resolve_test_database_url
from pydantic import SecretStr


def _settings() -> Settings:
    return Settings(database={"url": resolve_test_database_url()})


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
    assert observed["use_responses_api"] is False
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


def test_runtime_deadline_timeout_reaches_langchain_provider_call(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def fake_generate(
        _self: Any,
        _messages: Any,
        stop: Any = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        _ = stop, run_manager
        observed.update(kwargs)
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="ok"))]
        )

    monkeypatch.setattr(client_module._UsageAwareChatOpenAI, "_generate", fake_generate)
    client = LangChainChatClient(
        base_url="https://models.example.test/v1",
        api_key=SecretStr("test-key"),
        model_name="selected-model",
    )
    try:
        model = client.build_chat_model(temperature=None, max_tokens=128)
        response = invoke_model(
            model,
            [HumanMessage(content="ping")],
            timeout_seconds=17.5,
        )
    finally:
        client.close()

    assert response.content == "ok"
    assert observed["timeout"] == 17.5


def test_unknown_sync_signature_type_error_never_replays_partial_output() -> None:
    class UnknownSignatureModel:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, _messages: object, **kwargs: Any) -> None:
            self.calls += 1
            config = kwargs.get("config")
            if isinstance(config, dict):
                for callback in config.get("callbacks", []):
                    callback.on_llm_new_token(token="partial")
            raise TypeError("got an unexpected keyword argument 'config'")

    UnknownSignatureModel.invoke.__signature__ = object()
    model = UnknownSignatureModel()
    attempt = InvocationAttempt()

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        invoke_model(
            model,
            [],
            callbacks=[attempt],
            include_empty_callbacks=True,
        )

    assert attempt.emitted_tokens is True
    assert attempt.output_observable is False
    assert model.calls == 1


def test_positional_only_config_is_not_passed_as_a_keyword() -> None:
    class PositionalOnlyConfigModel:
        def invoke(
            self,
            messages: object,
            config: object = None,
            /,
            **kwargs: object,
        ) -> tuple[object, object, dict[str, object]]:
            return messages, config, kwargs

    attempt = InvocationAttempt()
    messages = [HumanMessage(content="ping")]

    result = invoke_model(
        PositionalOnlyConfigModel(),
        messages,
        callbacks=[attempt],
        include_empty_callbacks=True,
    )

    assert result == (messages, None, {})
    assert attempt.output_observable is False


def test_positional_only_timeout_is_not_passed_as_a_keyword() -> None:
    class PositionalOnlyTimeoutModel:
        def invoke(
            self,
            messages: object,
            timeout: float | None = None,
            /,
            **kwargs: object,
        ) -> tuple[object, object, dict[str, object]]:
            return messages, timeout, kwargs

    messages = [HumanMessage(content="ping")]

    result = invoke_model(
        PositionalOnlyTimeoutModel(),
        messages,
        timeout_seconds=17.5,
    )

    assert result == (messages, None, {})


def test_variadic_sync_config_support_is_treated_as_unobservable() -> None:
    class VariadicConfigModel:
        def invoke(self, _messages: object, **_ignored: Any) -> None:
            raise httpx.RemoteProtocolError("output may already have escaped")

    attempt = InvocationAttempt()

    with pytest.raises(httpx.RemoteProtocolError):
        invoke_model(
            VariadicConfigModel(),
            [],
            callbacks=[attempt],
            include_empty_callbacks=True,
        )

    assert attempt.output_observable is False


def test_unknown_async_signature_type_error_never_replays_partial_output() -> None:
    class UnknownSignatureModel:
        def __init__(self) -> None:
            self.calls = 0

        async def ainvoke(self, _messages: object, **kwargs: Any) -> None:
            self.calls += 1
            config = kwargs.get("config")
            if isinstance(config, dict):
                for callback in config.get("callbacks", []):
                    callback.on_llm_new_token(token="partial")
            raise TypeError("got an unexpected keyword argument 'config'")

    UnknownSignatureModel.ainvoke.__signature__ = object()
    model = UnknownSignatureModel()
    attempt = InvocationAttempt()

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        asyncio.run(
            ainvoke_model(
                model,
                [],
                callbacks=[attempt],
                include_empty_callbacks=True,
            )
        )

    assert attempt.emitted_tokens is True
    assert attempt.output_observable is False
    assert model.calls == 1


def test_variadic_async_config_support_is_treated_as_unobservable() -> None:
    class VariadicConfigModel:
        async def ainvoke(self, _messages: object, **_ignored: Any) -> None:
            raise httpx.RemoteProtocolError("output may already have escaped")

    attempt = InvocationAttempt()

    with pytest.raises(httpx.RemoteProtocolError):
        asyncio.run(
            ainvoke_model(
                VariadicConfigModel(),
                [],
                callbacks=[attempt],
                include_empty_callbacks=True,
            )
        )

    assert attempt.output_observable is False


def test_explicit_chat_mode_overrides_model_name_responses_inference(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            observed.update(kwargs)

    monkeypatch.setattr(
        "contentai.agent.infrastructure.llm.client._UsageAwareChatOpenAI", FakeChatOpenAI
    )
    client = LangChainChatClient(
        base_url="https://models.example.test/v1",
        api_key=SecretStr("test-key"),
        model_name="gpt-5.4-pro",
    )

    client.build_chat_model(temperature=None, max_tokens=128)

    assert observed["use_responses_api"] is False
    client.close()


def test_responses_runtime_payload_stays_aligned_with_probe_contract() -> None:
    client = LangChainChatClient(
        base_url="https://models.example.test/v1",
        api_key=SecretStr("test-key"),
        model_name="responses-model",
        api_mode="responses",
    )
    try:
        model = client.build_chat_model(temperature=None, max_tokens=1)
        payload = model._get_request_payload(
            [HumanMessage(content="ping")],
            stream=False,
        )
    finally:
        client.close()

    assert payload == {
        "model": "responses-model",
        "stream": False,
        "max_output_tokens": 1,
        "input": [{"content": "ping", "role": "user", "type": "message"}],
    }


def test_chat_completions_runtime_payload_stays_aligned_with_probe_contract() -> None:
    client = LangChainChatClient(
        base_url="https://models.example.test/v1",
        api_key=SecretStr("test-key"),
        model_name="chat-model",
        api_mode="chat_completions",
    )
    try:
        model = client.build_chat_model(temperature=None, max_tokens=1)
        payload = model._get_request_payload(
            [HumanMessage(content="ping")],
            stream=False,
        )
    finally:
        client.close()

    assert payload == {
        "model": "chat-model",
        "stream": False,
        "max_completion_tokens": 1,
        "messages": [{"content": "ping", "role": "user"}],
    }


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
        api_mode="responses",
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
        api_mode="responses",
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
    assert observed[0]["max_retries"] == 0
    assert all(call["max_retries"] == 0 for call in observed[1:4])
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
    assert observed["max_retries"] == 0
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


def _model_http_status_error(
    status_code: int,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://models.example.test/v1/chat/completions")
    response = httpx.Response(status_code, request=request, headers=headers)
    return httpx.HTTPStatusError(
        f"provider returned HTTP {status_code}",
        request=request,
        response=response,
    )


@pytest.mark.parametrize("status_code", [408, 409, 429, 500, 503, 599])
def test_transient_model_http_status_errors_are_retryable(status_code: int) -> None:
    error = _model_http_status_error(status_code)

    assert is_retryable_model_stream_error(error) is True
    assert classify_runtime_error(error).retryable is True


@pytest.mark.parametrize("status_code", [400, 401, 404, 422, 499, 600])
def test_permanent_model_http_status_errors_are_not_retryable(status_code: int) -> None:
    assert is_retryable_model_stream_error(_model_http_status_error(status_code)) is False


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Retry-After": "3"}, 3.0),
        ({"retry-after-ms": "1500", "Retry-After": "9"}, 1.5),
        ({"retry-after-ms": "invalid", "Retry-After": "4"}, 4.0),
    ],
)
def test_model_retry_delay_honors_provider_headers(
    headers: dict[str, str],
    expected: float,
) -> None:
    error = _model_http_status_error(429, headers=headers)

    assert model_retry_delay_seconds(
        error,
        attempt=1,
        base_seconds=0.25,
        max_seconds=30.0,
        random_value=0.0,
    ) == expected


def test_model_retry_delay_parses_http_date() -> None:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    error = _model_http_status_error(
        503,
        headers={"Retry-After": "Tue, 04 Aug 2026 12:00:07 GMT"},
    )

    assert model_retry_delay_seconds(
        error,
        attempt=1,
        base_seconds=0.25,
        max_seconds=30.0,
        random_value=0.0,
        now=now,
    ) == 7.0


def test_model_retry_delay_falls_back_to_exponential_backoff_with_bounded_jitter() -> None:
    error = _model_http_status_error(503, headers={"Retry-After": "invalid"})

    lower = model_retry_delay_seconds(
        error,
        attempt=3,
        base_seconds=0.25,
        max_seconds=30.0,
        random_value=0.0,
    )
    upper = model_retry_delay_seconds(
        error,
        attempt=3,
        base_seconds=0.25,
        max_seconds=30.0,
        random_value=1.0,
    )

    assert lower == 1.0
    assert upper == 1.25


def test_model_retry_delay_caps_large_provider_hint() -> None:
    error = _model_http_status_error(429, headers={"Retry-After": "999"})

    assert model_retry_delay_seconds(
        error,
        attempt=1,
        base_seconds=0.25,
        max_seconds=5.0,
        random_value=1.0,
    ) == 5.0


@pytest.mark.parametrize(
    "error",
    [
        httpx.RemoteProtocolError("incomplete chunked read"),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("connection refused"),
        httpx.ReadError("read failed"),
        httpx.WriteError("write failed"),
        httpx.PoolTimeout("pool exhausted"),
        httpx.ProxyError("proxy failed"),
    ],
)
def test_model_stream_transport_errors_are_retryable(error: Exception):
    detail = classify_runtime_error(error)

    assert detail.message == MODEL_STREAM_INTERRUPTED_MESSAGE
    assert detail.code == MODEL_STREAM_INTERRUPTED_CODE
    assert detail.retryable is True
