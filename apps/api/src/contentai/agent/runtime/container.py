from __future__ import annotations

import asyncio
import threading
import weakref
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from contentai.agent.graph.factory import build_agent_graph
from contentai.agent.infrastructure.llm import ModelGateway
from contentai.agent.runtime.checkpoint import RuntimePersistence, execution_checkpoint_config
from contentai.agent.tools.registry import ToolRegistry, tool_names
from contentai.core.config import Settings
from contentai.db.session import get_engine
from contentai.services.model_configuration_service import ModelConfigurationService
from langchain_core.tools import BaseTool
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
    def __init__(
        self,
        gateway: ModelGateway,
        *,
        close_when_retired: bool,
        on_closed: Callable[[_GatewayOwner], None] | None = None,
    ) -> None:
        self.gateway = gateway
        self.close_when_retired = close_when_retired
        self._on_closed = on_closed
        self._lock = threading.Lock()
        self._close_complete = threading.Event()
        self._close_complete.set()
        self._references = 0
        self._retired = False
        self._state = "active"
        self._close_error: BaseException | None = None
        self._close_generation = 0
        self._close_monitor: asyncio.Task[None] | None = None
        self._close_monitor_token: object | None = None

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._state == "closed"

    def retain(self) -> None:
        with self._lock:
            self._references += 1

    def release(self) -> None:
        with self._lock:
            self._references -= 1
            should_close = self._can_close_locked()
        if should_close:
            try:
                self.close()
            except Exception:
                pass

    def retire(self) -> bool:
        with self._lock:
            self._retired = True
            if not self.close_when_retired:
                self._settle_injected_locked()
                return True
            should_close = self._can_close_locked()
            if not should_close:
                return self._state == "closed"
        return self.close()

    def close(self) -> bool:
        with self._lock:
            self._retired = True
            if not self.close_when_retired:
                self._settle_injected_locked()
                return True
            if self._state == "closed":
                return True
            if not self._can_close_locked():
                return False
            self._begin_close_locked()
        try:
            completed = self.gateway.close()
        except Exception as exc:
            self._finish_close(error=exc)
            raise
        if completed is False:
            if self._schedule_close_monitor():
                return False
            self._finish_close(error=None, pending=True)
            return False
        self._finish_close(error=None)
        return True

    async def aclose(self) -> None:
        while True:
            with self._lock:
                self._retired = True
                if not self.close_when_retired:
                    self._settle_injected_locked()
                    return
                if self._state == "closed":
                    return
                if self._references != 0:
                    raise RuntimeError(
                        "Cannot close a gateway owner with retained runtime values."
                    )
                if self._state == "closing":
                    close_complete = self._close_complete
                else:
                    self._begin_close_locked()
                    break
            await asyncio.to_thread(close_complete.wait)
        try:
            await self.gateway.aclose()
        except asyncio.CancelledError as exc:
            self._finish_close(error=exc)
            raise
        except Exception as exc:
            self._finish_close(error=exc)
            raise
        self._finish_close(error=None)

    def _schedule_close_monitor(self) -> bool:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        with self._lock:
            if self._state != "closing":
                return False
            if self._close_monitor_token is not None:
                return True
            generation = self._close_generation
            monitor_token = object()
            self._close_monitor_token = monitor_token
        monitor_coroutine = self._monitor_async_close(
            generation,
            monitor_token,
        )
        try:
            monitor_task = loop.create_task(
                monitor_coroutine,
            )
        except BaseException as exc:
            monitor_coroutine.close()
            self._finish_close(
                error=exc,
                generation=generation,
                monitor_token=monitor_token,
            )
            raise
        monitor_task.add_done_callback(
            lambda completed: self._finish_unstarted_monitor(
                completed,
                generation=generation,
                monitor_token=monitor_token,
            )
        )
        with self._lock:
            if (
                generation == self._close_generation
                and monitor_token is self._close_monitor_token
            ):
                self._close_monitor = monitor_task
        return True

    async def _monitor_async_close(
        self,
        generation: int,
        monitor_token: object,
    ) -> None:
        try:
            await self.gateway.aclose()
        except asyncio.CancelledError as exc:
            self._finish_close(
                error=exc,
                generation=generation,
                monitor_token=monitor_token,
            )
        except Exception as exc:
            self._finish_close(
                error=exc,
                generation=generation,
                monitor_token=monitor_token,
            )
        else:
            self._finish_close(
                error=None,
                generation=generation,
                monitor_token=monitor_token,
            )

    def _finish_unstarted_monitor(
        self,
        monitor_task: asyncio.Task[None],
        *,
        generation: int,
        monitor_token: object,
    ) -> None:
        if not monitor_task.cancelled():
            return
        self._finish_close(
            error=asyncio.CancelledError(),
            generation=generation,
            monitor_token=monitor_token,
        )

    def _begin_close_locked(self) -> None:
        self._close_generation += 1
        self._close_monitor = None
        self._close_monitor_token = None
        self._state = "closing"
        self._close_error = None
        self._close_complete.clear()

    def _finish_close(
        self,
        *,
        error: BaseException | None,
        pending: bool = False,
        generation: int | None = None,
        monitor_token: object | None = None,
    ) -> None:
        on_closed: Callable[[_GatewayOwner], None] | None = None
        with self._lock:
            if generation is not None and (
                generation != self._close_generation
                or monitor_token is not self._close_monitor_token
            ):
                return
            self._close_error = error
            if monitor_token is not None:
                self._close_monitor = None
                self._close_monitor_token = None
            if error is not None:
                self._state = "active"
            elif pending:
                self._state = "pending"
            else:
                self._state = "closed"
                on_closed = self._on_closed
            self._close_complete.set()
        if on_closed is not None:
            on_closed(self)

    def _settle_injected_locked(self) -> None:
        self._state = "closed"
        self._close_error = None
        self._close_complete.set()

    def _can_close_locked(self) -> bool:
        return (
            self.close_when_retired
            and self._retired
            and self._state in {"active", "pending"}
            and self._references == 0
        )


class _RetainedRuntimeValue:
    def __init__(self, value: Any, owner: _GatewayOwner) -> None:
        self._value = value
        # Let signature-aware callers inspect the retained value instead of
        # mistaking this generic ``*args, **kwargs`` proxy for provider support.
        self.__wrapped__ = value
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

    def retire(self, *, explicit_close: bool = False) -> bool:
        if explicit_close:
            self.gateway = None
            self.models.clear()
            self.graphs.clear()
            return self.owner.retire()
        try:
            return self.owner.retire()
        finally:
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
    _retired_owners: set[_GatewayOwner] = field(default_factory=set, init=False)
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
                api_mode=configuration.api_mode,
                temperature=configuration.temperature,
                context_window_tokens=configuration.context_window_tokens,
                chat_max_tokens=configuration.chat_max_tokens,
                structured_max_tokens=configuration.structured_max_tokens,
            )
            close_when_retired = True
        owner = _GatewayOwner(
            gateway,
            close_when_retired=close_when_retired,
            on_closed=self._discard_retired_owner,
        )
        entry = _RuntimeCacheEntry(
            owner=owner,
            gateway=_RetainedRuntimeValue(gateway, owner),
        )
        self._runtime_entries[model_config_id] = entry
        self._runtime_entries.move_to_end(model_config_id)
        while len(self._runtime_entries) > self._cache_capacity:
            _, evicted = self._runtime_entries.popitem(last=False)
            self._retire_entry(evicted)
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
        built_graph = build_agent_graph(
            model=model,
            tools=tools,
            research_final_model=lambda: self._build_research_final_model(entry),
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
        errors: list[Exception] = []
        failed_owners: set[_GatewayOwner] = set()
        with self._cache_lock:
            entries = tuple(self._runtime_entries.values())
            self._runtime_entries.clear()
            for entry in entries:
                try:
                    self._retire_entry(entry, explicit_close=True)
                except Exception as exc:
                    errors.append(exc)
                    failed_owners.add(entry.owner)
            owners = tuple(self._retired_owners)
        for owner in owners:
            if owner in failed_owners:
                continue
            try:
                owner.close()
            except Exception as exc:
                errors.append(exc)
            if owner.closed:
                with self._cache_lock:
                    self._retired_owners.discard(owner)
        try:
            self._close_owned_persistence()
        except Exception as exc:
            errors.append(exc)
        if errors:
            raise errors[0]

    async def aclose(self) -> None:
        errors: list[Exception] = []
        failed_owners: set[_GatewayOwner] = set()
        with self._cache_lock:
            entries = tuple(self._runtime_entries.values())
            self._runtime_entries.clear()
            for entry in entries:
                try:
                    self._retire_entry(entry)
                except Exception as exc:
                    errors.append(exc)
                    failed_owners.add(entry.owner)
            owners = tuple(self._retired_owners)
        for owner in owners:
            if owner in failed_owners:
                continue
            try:
                await owner.aclose()
            except Exception as exc:
                errors.append(exc)
            if owner.closed:
                with self._cache_lock:
                    self._retired_owners.discard(owner)
        try:
            self._close_owned_persistence()
        except Exception as exc:
            errors.append(exc)
        if errors:
            raise errors[0]

    def _retire_entry(
        self,
        entry: _RuntimeCacheEntry,
        *,
        explicit_close: bool = False,
    ) -> None:
        owner = entry.owner
        try:
            entry.retire(explicit_close=explicit_close)
        finally:
            if owner.closed:
                self._retired_owners.discard(owner)
            else:
                self._retired_owners.add(owner)

    def _discard_retired_owner(self, owner: _GatewayOwner) -> None:
        with self._cache_lock:
            self._retired_owners.discard(owner)

    def _close_owned_persistence(self) -> None:
        if self._owns_checkpointer and self.persistence is not None:
            self.persistence.close()
            self._owns_checkpointer = False
            self.checkpointer = None
