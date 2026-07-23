from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import Future
from typing import Any

import httpx
from langchain_openai import ChatOpenAI
from pydantic import SecretStr
from services.model_config_network import PinnedAsyncModelTransport, PinnedModelTransport

_STREAM_END = object()


class _OwnerLoopAsyncByteStream(httpx.AsyncByteStream):
    def __init__(
        self,
        stream: httpx.AsyncByteStream,
        owner_loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._stream = stream
        self._owner_loop = owner_loop

    async def _run_on_owner_loop(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        if asyncio.get_running_loop() is self._owner_loop:
            return await operation()
        future = asyncio.run_coroutine_threadsafe(operation(), self._owner_loop)
        return await asyncio.wrap_future(future)

    async def _create_iterator(self) -> AsyncIterator[bytes]:
        return self._stream.__aiter__()

    @staticmethod
    async def _next_chunk(iterator: AsyncIterator[bytes]) -> bytes | object:
        try:
            return await anext(iterator)
        except StopAsyncIteration:
            return _STREAM_END

    async def __aiter__(self) -> AsyncIterator[bytes]:
        iterator = await self._run_on_owner_loop(self._create_iterator)
        while True:
            chunk = await self._run_on_owner_loop(lambda: self._next_chunk(iterator))
            if chunk is _STREAM_END:
                return
            yield chunk

    async def aclose(self) -> None:
        await self._run_on_owner_loop(self._stream.aclose)


class _OwnerLoopAsyncClient(httpx.AsyncClient):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._owner_ready = threading.Event()
        self._owner_stopped = threading.Event()
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._close_state_lock = threading.Lock()
        self._close_future: Future[None] | None = None
        self._close_generation = 0
        self._async_closed = False
        self._transport_closed = False
        self._closed_mount_ids: set[int] = set()
        self._owner_thread = threading.Thread(
            target=self._run_owner_loop,
            name="model-http-async-owner",
            daemon=True,
        )
        self._owner_thread.start()
        if not self._owner_ready.wait(timeout=5.0):
            raise RuntimeError("Model HTTP async owner loop did not start.")

    def _run_owner_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._owner_loop = loop
        self._owner_ready.set()
        try:
            loop.run_forever()
        finally:
            loop.close()
            self._owner_stopped.set()

    def _require_owner_loop(self) -> asyncio.AbstractEventLoop:
        loop = self._owner_loop
        if loop is None or self._owner_stopped.is_set():
            raise RuntimeError("Model HTTP async owner loop is not available.")
        return loop

    async def send(
        self,
        request: httpx.Request,
        *,
        stream: bool = False,
        auth: Any = httpx.USE_CLIENT_DEFAULT,
        follow_redirects: Any = httpx.USE_CLIENT_DEFAULT,
    ) -> httpx.Response:
        owner_loop = self._require_owner_loop()
        if asyncio.get_running_loop() is owner_loop:
            return await self._send_on_owner_loop(
                request,
                stream=stream,
                auth=auth,
                follow_redirects=follow_redirects,
                owner_loop=owner_loop,
            )
        future = asyncio.run_coroutine_threadsafe(
            self._send_on_owner_loop(
                request,
                stream=stream,
                auth=auth,
                follow_redirects=follow_redirects,
                owner_loop=owner_loop,
            ),
            owner_loop,
        )
        return await asyncio.wrap_future(future)

    async def _send_on_owner_loop(
        self,
        request: httpx.Request,
        *,
        stream: bool,
        auth: Any,
        follow_redirects: Any,
        owner_loop: asyncio.AbstractEventLoop,
    ) -> httpx.Response:
        response = await super().send(
            request,
            stream=stream,
            auth=auth,
            follow_redirects=follow_redirects,
        )
        if stream:
            response.stream = _OwnerLoopAsyncByteStream(response.stream, owner_loop)
        return response

    async def _close_on_owner_loop(self) -> None:
        if self._async_closed:
            return
        if not self._transport_closed:
            await self._transport.aclose()
            self._transport_closed = True
        for proxy in self._mounts.values():
            if proxy is None or id(proxy) in self._closed_mount_ids:
                continue
            await proxy.aclose()
            self._closed_mount_ids.add(id(proxy))
        self._state = self._state.__class__.CLOSED

    def _submit_close(self) -> Future[None]:
        with self._close_state_lock:
            if self._async_closed:
                completed: Future[None] = Future()
                completed.set_result(None)
                return completed
            if self._close_future is not None:
                return self._close_future
            future = asyncio.run_coroutine_threadsafe(
                self._close_on_owner_loop(),
                self._require_owner_loop(),
            )
            self._close_generation += 1
            generation = self._close_generation
            self._close_future = future
        future.add_done_callback(
            lambda completed: self._finish_close(completed, generation=generation)
        )
        return future

    def _finish_close(self, future: Future[None], *, generation: int) -> None:
        succeeded = not future.cancelled() and future.exception() is None
        should_stop = False
        with self._close_state_lock:
            if future is not self._close_future or generation != self._close_generation:
                return
            if succeeded:
                self._async_closed = True
                should_stop = True
            else:
                self._close_future = None
        if should_stop:
            owner_loop = self._require_owner_loop()
            owner_loop.call_soon_threadsafe(owner_loop.stop)

    def close_from_sync(self) -> bool:
        future = self._submit_close()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            future.result()
            self._owner_thread.join(timeout=5.0)
            return True
        return future.done() and future.exception() is None

    async def aclose(self) -> None:
        future = self._submit_close()
        await asyncio.wrap_future(future)


class LangChainChatClient:
    """Build OpenAI-compatible chat clients from one immutable configuration."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        model_name: str,
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._model_name = model_name
        self._http_client = httpx.Client(
            transport=PinnedModelTransport(base_url=base_url),
            follow_redirects=False,
            trust_env=False,
        )
        self._http_async_client = _OwnerLoopAsyncClient(
            transport=PinnedAsyncModelTransport(base_url=base_url),
            follow_redirects=False,
            trust_env=False,
        )
        self._close_lock = threading.Lock()
        self._sync_closed = False
        self._async_closed = False

    def _close_sync_client(self) -> None:
        with self._close_lock:
            if self._sync_closed:
                return
            self._http_client.close()
            self._sync_closed = True

    def close(self) -> bool:
        self._close_sync_client()
        with self._close_lock:
            if self._async_closed:
                return True
            completed = self._http_async_client.close_from_sync()
            if completed:
                self._async_closed = True
            return completed

    async def aclose(self) -> None:
        self._close_sync_client()
        with self._close_lock:
            if self._async_closed:
                return
        await self._http_async_client.aclose()
        with self._close_lock:
            self._async_closed = True

    def build_chat_model(
        self,
        *,
        temperature: float,
        max_tokens: int,
        model: str | None = None,
        tools: list[Any] | None = None,
        timeout_seconds: float = 240.0,
        max_retries: int = 2,
        disable_streaming: bool = False,
    ) -> Any:
        selected_model = model or self._model_name
        if selected_model != self._model_name:
            raise ValueError("The requested model does not match the execution configuration.")
        chat_model = ChatOpenAI(
            model=selected_model,
            api_key=self._api_key,
            base_url=self._base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=max(0.1, float(timeout_seconds)),
            max_retries=max(0, int(max_retries)),
            disable_streaming=disable_streaming,
            http_client=self._http_client,
            http_async_client=self._http_async_client,
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
        temperature: float,
        max_tokens: int,
        schema: type[Any],
        model: str | None = None,
        timeout_seconds: float = 240.0,
        max_retries: int = 2,
        disable_streaming: bool = False,
    ) -> Any:
        chat_model = self.build_chat_model(
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
            disable_streaming=disable_streaming,
        )
        return chat_model.with_structured_output(schema)
