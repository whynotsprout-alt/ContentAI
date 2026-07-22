from __future__ import annotations

import asyncio
import threading
from typing import Any

import httpx
from langchain_openai import ChatOpenAI
from pydantic import SecretStr
from services.model_config_network import PinnedAsyncModelTransport, PinnedModelTransport


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
        self._http_async_client = httpx.AsyncClient(
            transport=PinnedAsyncModelTransport(base_url=base_url),
            follow_redirects=False,
            trust_env=False,
        )
        self._close_lock = threading.Lock()
        self._closed = False

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True

        self._http_client.close()

        def close_async_client() -> None:
            asyncio.run(self._http_async_client.aclose())

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            close_async_client()
            return

        errors: list[BaseException] = []

        def close_in_thread() -> None:
            try:
                close_async_client()
            except BaseException as exc:  # pragma: no cover - defensive shutdown propagation
                errors.append(exc)

        thread = threading.Thread(
            target=close_in_thread,
            name="model-http-client-close",
            daemon=True,
        )
        thread.start()
        thread.join()
        if errors:
            raise errors[0]

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
