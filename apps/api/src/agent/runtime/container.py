from __future__ import annotations

import threading
import weakref
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent.graph.factory import build_agent_graph
from agent.infrastructure.llm import ModelGateway
from agent.runtime.checkpoint import RuntimePersistence, execution_checkpoint_config
from agent.tools.registry import ToolRegistry, tool_names
from core.config import Settings
from db.session import get_engine
from langchain_core.tools import BaseTool
from services.model_configuration_service import ModelConfigurationService
from sqlmodel import Session


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


class _GatewayOwner:
    def __init__(self, gateway: ModelGateway, *, close_when_retired: bool) -> None:
        self.gateway = gateway
        self.close_when_retired = close_when_retired
        self._lock = threading.Lock()
        self._references = 0
        self._retired = False
        self._closed = False

    def retain(self) -> None:
        with self._lock:
            self._references += 1

    def release(self) -> None:
        should_close = False
        with self._lock:
            self._references -= 1
            should_close = self._should_close()
            if should_close:
                self._closed = True
        if should_close:
            self.gateway.close()

    def retire(self) -> None:
        should_close = False
        with self._lock:
            self._retired = True
            should_close = self._should_close()
            if should_close:
                self._closed = True
        if should_close:
            self.gateway.close()

    def _should_close(self) -> bool:
        return (
            self.close_when_retired
            and self._retired
            and not self._closed
            and self._references == 0
        )


class _RetainedRuntimeValue:
    def __init__(self, value: Any, owner: _GatewayOwner) -> None:
        self._value = value
        owner.retain()
        self._release = weakref.finalize(self, owner.release)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._value, name)

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        return self._value.invoke(*args, **kwargs)

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        yield from self._value.stream(*args, **kwargs)

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        return await self._value.ainvoke(*args, **kwargs)


@dataclass
class _RuntimeCacheEntry:
    owner: _GatewayOwner
    gateway: _RetainedRuntimeValue | None
    models: OrderedDict[tuple[str, ...], AgentModel] = field(default_factory=OrderedDict)
    graphs: OrderedDict[tuple[str, ...], AgentGraph] = field(default_factory=OrderedDict)

    def retire(self) -> None:
        self.owner.retire()
        self.gateway = None
        self.models.clear()
        self.graphs.clear()


@dataclass(frozen=True)
class AgentRuntime:
    graph: AgentGraph
    gateway: ModelGateway
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
    side_effect_dispatcher: Callable[[dict[str, Any]], None] | None = None
    side_effect_receipt_poller: Callable[[str, str], dict[str, Any] | None] | None = None

    _runtime_entries: OrderedDict[str, _RuntimeCacheEntry] = field(
        default_factory=OrderedDict,
        init=False,
    )
    _cache_lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    _owns_checkpointer: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.tool_registry.default_timeout_seconds = self.settings.agent.tool_timeout_seconds
        if self.persistence is None:
            self.persistence = RuntimePersistence(self.settings)
        if self.checkpointer is not None:
            self._owns_checkpointer = False

    @property
    def graph(self) -> AgentGraph:
        raise RuntimeError("model_config_id is required to build an execution graph.")

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
        model_config_id: str,
        tool_permissions: Sequence[str] | None = None,
    ) -> AgentModel:
        tools = self.get_tools(tool_permissions)
        cache_key = self._runtime_cache_key(
            model_config_id=model_config_id,
            tool_permissions=tool_permissions,
            tools=tools,
        )
        with self._cache_lock:
            entry = self._entry_for_model_config(model_config_id)
            return self._model_for_entry(entry, cache_key=cache_key, tools=tools)

    def get_checkpointer(self) -> Checkpointer:
        if self.checkpointer is None:
            if self.persistence is None:
                raise RuntimeError("Runtime persistence is not configured.")
            self.checkpointer = self.persistence.get_checkpointer()
            self._owns_checkpointer = True
        return self.checkpointer

    def graph_for_permissions(
        self,
        model_config_id: str,
        tool_permissions: Sequence[str] | None = None,
    ) -> AgentGraph:
        tools = self.get_tools(tool_permissions)
        cache_key = self._runtime_cache_key(
            model_config_id=model_config_id,
            tool_permissions=tool_permissions,
            tools=tools,
        )
        with self._cache_lock:
            entry = self._entry_for_model_config(model_config_id)
            return self._graph_for_entry(
                entry,
                cache_key=cache_key,
                tools=tools,
            )

    @staticmethod
    def _build_research_final_model(entry: _RuntimeCacheEntry) -> Any:
        build_research_final = getattr(entry.owner.gateway, "build_research_final_model", None)
        if not callable(build_research_final):
            raise RuntimeError("Model gateway does not support structured research final output.")
        return build_research_final()

    @staticmethod
    def _build_research_presentation_model(entry: _RuntimeCacheEntry) -> Any:
        build_research_presentation = getattr(
            entry.owner.gateway, "build_research_presentation_model", None
        )
        if not callable(build_research_presentation):
            raise RuntimeError(
                "Model gateway does not support structured research presentation output."
            )
        return build_research_presentation()

    def gateway_for_model_config(self, model_config_id: str) -> ModelGateway:
        if not model_config_id:
            raise RuntimeError("model_config_id is required.")
        with self._cache_lock:
            entry = self._entry_for_model_config(model_config_id)
            if entry.gateway is None:
                entry.gateway = _RetainedRuntimeValue(entry.owner.gateway, entry.owner)
            return entry.gateway

    def _entry_for_model_config(self, model_config_id: str) -> _RuntimeCacheEntry:
        cached = self._runtime_entries.get(model_config_id)
        if cached is not None:
            self._runtime_entries.move_to_end(model_config_id)
            return cached
        if self.model_gateway is not None:
            gateway = self.model_gateway
            close_when_retired = False
        else:
            with Session(get_engine(self.settings)) as session:
                configuration = ModelConfigurationService(self.settings).get_runtime_by_id(
                    session,
                    model_config_id,
                )
            gateway = ModelGateway(
                model_config_id=configuration.id,
                base_url=configuration.base_url,
                api_key=configuration.api_key,
                model_name=configuration.model_name,
                temperature=configuration.temperature,
                context_window_tokens=configuration.context_window_tokens,
                chat_max_tokens=configuration.chat_max_tokens,
                structured_max_tokens=configuration.structured_max_tokens,
            )
            close_when_retired = True
        owner = _GatewayOwner(gateway, close_when_retired=close_when_retired)
        entry = _RuntimeCacheEntry(
            owner=owner,
            gateway=_RetainedRuntimeValue(gateway, owner),
        )
        self._runtime_entries[model_config_id] = entry
        self._runtime_entries.move_to_end(model_config_id)
        while len(self._runtime_entries) > self._cache_capacity:
            _, evicted = self._runtime_entries.popitem(last=False)
            evicted.retire()
        return entry

    def _model_for_entry(
        self,
        entry: _RuntimeCacheEntry,
        *,
        cache_key: tuple[str, ...],
        tools: list[BaseTool],
    ) -> AgentModel:
        model = self._cache_get(entry.models, cache_key)
        if model is not None:
            return model
        built_model = entry.owner.gateway.build_agent_model(tools=tools)
        if not callable(getattr(built_model, "invoke", None)):
            raise TypeError(
                f"Model gateway returned non-invokable model type: {type(built_model)!r}."
            )
        model = _RetainedRuntimeValue(built_model, entry.owner)
        self._cache_put(entry.models, cache_key, model)
        return model

    def _tool_intent_model_for_entry(
        self,
        entry: _RuntimeCacheEntry,
        *,
        cache_key: tuple[str, ...],
    ) -> Any | None:
        """Build one structured intent gate per model/tool-permission cache key."""
        intent_cache_key = ("tool_intent", *cache_key)
        model = self._cache_get(entry.models, intent_cache_key)
        if model is not None:
            return model
        build_structured = getattr(entry.owner.gateway, "build_structured_output_model", None)
        if not callable(build_structured):
            return None
        from agent.graph.nodes import ToolIntentDecision

        built_model = build_structured(ToolIntentDecision)
        if not callable(getattr(built_model, "invoke", None)):
            raise TypeError(
                "Model gateway returned a non-invokable structured tool-intent model."
            )
        model = _RetainedRuntimeValue(built_model, entry.owner)
        self._cache_put(entry.models, intent_cache_key, model)
        return model

    def _graph_for_entry(
        self,
        entry: _RuntimeCacheEntry,
        *,
        cache_key: tuple[str, ...],
        tools: list[BaseTool],
    ) -> AgentGraph:
        graph = self._cache_get(entry.graphs, cache_key)
        if graph is not None:
            return graph
        model = self._model_for_entry(entry, cache_key=cache_key, tools=tools)
        tool_intent_model = self._tool_intent_model_for_entry(entry, cache_key=cache_key)
        built_graph = build_agent_graph(
            model=model,
            tools=tools,
            research_final_model=lambda: self._build_research_final_model(entry),
            research_presentation_model=lambda: self._build_research_presentation_model(entry),
            tool_intent_model=tool_intent_model,
            checkpointer=self.get_checkpointer(),
        )
        graph = _RetainedRuntimeValue(built_graph, entry.owner)
        self._cache_put(entry.graphs, cache_key, graph)
        return graph

    def create_runtime(
        self,
        *,
        model_config_id: str,
        tool_permissions: Sequence[str] | None = None,
        user_id: str,
        agent_id: str,
        session_id: str,
        conversation_id: str | None = None,
        execution_id: str | None = None,
    ) -> AgentRuntime:
        tools = self.get_tools(tool_permissions)
        configurable: dict[str, str] = {
            "session_id": session_id,
            "user_id": user_id,
            "agent_id": agent_id,
        }
        if conversation_id is not None:
            configurable["conversation_id"] = conversation_id
        if execution_id is not None:
            configurable["execution_id"] = execution_id
            configurable.update(
                execution_checkpoint_config(
                    thread_id=session_id,
                    execution_id=execution_id,
                )["configurable"]
            )
        else:
            configurable["thread_id"] = session_id
        cache_key = self._runtime_cache_key(
            model_config_id=model_config_id,
            tool_permissions=tool_permissions,
            tools=tools,
        )
        with self._cache_lock:
            entry = self._entry_for_model_config(model_config_id)
            graph = self._graph_for_entry(entry, cache_key=cache_key, tools=tools)
            if entry.gateway is None:
                entry.gateway = _RetainedRuntimeValue(entry.owner.gateway, entry.owner)
            gateway = entry.gateway
            return AgentRuntime(
                graph=graph,
                gateway=gateway,
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
        model_config_id: str,
        tools: Sequence[Any],
        tool_permissions: Sequence[str] | None,
    ) -> tuple[str, ...]:
        permission_signature = self._normalized_tool_signature(tool_permissions)
        resolved_tools = self._tool_names_signature(tools)
        return (
            "model_config_id",
            model_config_id,
            "permissions",
            *permission_signature,
            *resolved_tools,
        )

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
            entries = tuple(self._runtime_entries.values())
            self._runtime_entries.clear()
        for entry in entries:
            entry.retire()
        if self._owns_checkpointer and self.persistence is not None:
            self.persistence.close()
            self._owns_checkpointer = False
            self.checkpointer = None
