from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent.graph.factory import build_agent_graph
from agent.infrastructure.llm import ModelGateway
from agent.runtime.checkpoint import RuntimePersistence
from agent.tools.registry import ToolRegistry, tool_names
from core.config import Settings
from langchain_core.tools import BaseTool


class AgentModel(Protocol):
    def invoke(self, _input: Any) -> Any: ...
    def stream(self, _input: Any) -> Any: ...
    async def ainvoke(self, _input: Any, **kwargs: Any) -> Any: ...


class AgentGraph(Protocol):
    def stream(
        self,
        _input: Any,
        config: Mapping[str, Any] | None = None,
        stream_mode: Sequence[str] | None = None,
    ) -> Any: ...

    async def ainvoke(self, _input: Any, **kwargs: Any) -> Any: ...

    def invoke(self, _input: Any, **kwargs: Any) -> Any: ...


class Checkpointer(Protocol):
    def get_tuple(self, config: Mapping[str, Any]) -> Any: ...


@dataclass(frozen=True)
class AgentRuntime:
    graph: AgentGraph
    checkpointer: Checkpointer
    tool_permissions: tuple[str, ...]
    config: dict[str, Any]


@dataclass
class RuntimeContainer:
    settings: Settings
    model_gateway: ModelGateway | None = None
    tool_registry: ToolRegistry = field(default_factory=ToolRegistry)
    checkpointer: Checkpointer | None = None
    persistence: RuntimePersistence | None = None

    _model_cache: OrderedDict[tuple[str, ...], AgentModel] = field(
        default_factory=OrderedDict,
        init=False,
    )
    _graph_cache: OrderedDict[tuple[str, ...], AgentGraph] = field(
        default_factory=OrderedDict,
        init=False,
    )
    _cache_lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    _owns_checkpointer: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.tool_registry.default_timeout_seconds = self.settings.agent.tool_timeout_seconds
        if self.persistence is None:
            self.persistence = RuntimePersistence(self.settings)
        if self.model_gateway is None:
            self.model_gateway = ModelGateway(self.settings)
        if self.checkpointer is not None:
            self._owns_checkpointer = False

    @property
    def graph(self) -> AgentGraph:
        return self.graph_for_permissions(("*",))

    def get_tools(self, tool_permissions: Sequence[str] | None = None) -> list[BaseTool]:
        return self.tool_registry.get_tools(tool_permissions)

    @staticmethod
    def _normalized_tool_signature(
        tool_permissions: Sequence[str] | None = None,
    ) -> tuple[str, ...]:
        if tool_permissions is None:
            return ("*",)
        return tuple(sorted({str(item).strip() for item in tool_permissions if str(item).strip()}))

    def get_model(
        self,
        tool_permissions: Sequence[str] | None = None,
    ) -> AgentModel:
        tools = self.get_tools(tool_permissions)
        cache_key = self._runtime_cache_key(
            tool_permissions=tool_permissions,
            tools=tools,
        )
        model = self._cache_get(self._model_cache, cache_key)
        if model is not None:
            return model
        gateway = self.model_gateway
        if gateway is None:
            raise RuntimeError("Model gateway is not configured.")
        model = gateway.build_agent_model(tools=tools)
        if not callable(getattr(model, "invoke", None)):
            raise TypeError(f"Model gateway returned non-invokable model type: {type(model)!r}.")
        self._cache_put(self._model_cache, cache_key, model)
        return model

    def get_checkpointer(self) -> Checkpointer:
        if self.checkpointer is None:
            if self.persistence is None:
                raise RuntimeError("Runtime persistence is not configured.")
            self.checkpointer = self.persistence.get_checkpointer()
            self._owns_checkpointer = True
        return self.checkpointer

    def graph_for_permissions(
        self,
        tool_permissions: Sequence[str] | None = None,
    ) -> AgentGraph:
        tools = self.get_tools(tool_permissions)
        cache_key = self._runtime_cache_key(
            tool_permissions=tool_permissions,
            tools=tools,
        )
        graph = self._cache_get(self._graph_cache, cache_key)
        if graph is not None:
            return graph
        graph = build_agent_graph(
            model=self.get_model(tool_permissions),
            tools=tools,
            checkpointer=self.get_checkpointer(),
        )
        self._cache_put(self._graph_cache, cache_key, graph)
        return graph

    def create_runtime(
        self,
        *,
        tool_permissions: Sequence[str] | None = None,
        user_id: str,
        agent_id: str,
        session_id: str,
        conversation_id: str | None = None,
        execution_id: str | None = None,
    ) -> AgentRuntime:
        tools = self.get_tools(tool_permissions)
        configurable: dict[str, str] = {
            "thread_id": session_id,
            "session_id": session_id,
            "user_id": user_id,
            "agent_id": agent_id,
        }
        if conversation_id is not None:
            configurable["conversation_id"] = conversation_id
        if execution_id is not None:
            configurable["execution_id"] = execution_id
        return AgentRuntime(
            graph=self.graph_for_permissions(tool_permissions),
            checkpointer=self.get_checkpointer(),
            tool_permissions=tuple(self._tool_names_signature(tools)),
            config={
                "configurable": configurable,
                "recursion_limit": self.settings.agent.recursion_limit,
            },
        )

    @staticmethod
    def _tool_names_signature(tools: Sequence[Any]) -> tuple[str, ...]:
        return tuple(sorted(set(tool_names(tools))))

    def _runtime_cache_key(
        self,
        *,
        tools: Sequence[Any],
        tool_permissions: Sequence[str] | None,
    ) -> tuple[str, ...]:
        llm = self.settings.llm
        model_signature = (
            llm.chat_model,
            str(llm.temperature),
            str(llm.chat_max_tokens),
            str(self.settings.search.traffic_relay_base_url).rstrip("/"),
        )
        permission_signature = self._normalized_tool_signature(tool_permissions)
        resolved_tools = self._tool_names_signature(tools)
        return ("model", *model_signature, "permissions", *permission_signature, *resolved_tools)

    @property
    def _cache_capacity(self) -> int:
        return max(1, int(self.settings.agent.runtime_cache_capacity))

    def _cache_get[T](
        self,
        cache: OrderedDict[tuple[str, ...], T],
        key: tuple[str, ...],
    ) -> T | None:
        with self._cache_lock:
            value = cache.get(key)
            if value is not None:
                cache.move_to_end(key)
            return value

    def _cache_put[T](
        self,
        cache: OrderedDict[tuple[str, ...], T],
        key: tuple[str, ...],
        value: T,
    ) -> None:
        with self._cache_lock:
            cache[key] = value
            cache.move_to_end(key)
            while len(cache) > self._cache_capacity:
                cache.popitem(last=False)

    def close(self) -> None:
        with self._cache_lock:
            self._model_cache.clear()
            self._graph_cache.clear()
        if self._owns_checkpointer and self.persistence is not None:
            self.persistence.close()
            self._owns_checkpointer = False
            self.checkpointer = None
