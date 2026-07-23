from __future__ import annotations

import asyncio
import gc
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest
from agent.runtime.container import RuntimeContainer, _GatewayOwner
from core.config import get_settings
from pydantic import SecretStr


class _Model:
    def invoke(self, input: Any) -> Any:
        return input


class _Gateway:
    def __init__(self) -> None:
        self.build_count = 0

    def build_agent_model(self, *, tools: list[Any] | None = None) -> _Model:
        self.build_count += 1
        return _Model()


class _ClosableGateway:
    def __init__(self, *, model_config_id: str, **_kwargs: Any) -> None:
        self.model_config_id = model_config_id
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


class _FlakyClosableGateway(_ClosableGateway):
    def __init__(self, *, model_config_id: str, **kwargs: Any) -> None:
        super().__init__(model_config_id=model_config_id, **kwargs)
        self.close_attempts = 0

    def close(self) -> bool:
        self.close_attempts += 1
        if self.close_attempts == 1:
            raise RuntimeError("first close failed")
        return True

    async def aclose(self) -> None:
        self.close_attempts += 1


class _FlakyAsyncClosableGateway(_ClosableGateway):
    def __init__(self, *, model_config_id: str, **kwargs: Any) -> None:
        super().__init__(model_config_id=model_config_id, **kwargs)
        self.async_close_attempts = 0

    def close(self) -> bool:
        return False

    async def aclose(self) -> None:
        self.async_close_attempts += 1
        if self.async_close_attempts == 1:
            raise RuntimeError("first async close failed")


class _UsageTrackingModel:
    def __init__(self, gateway: _ClosableGateway) -> None:
        self.gateway = gateway

    def invoke(self, input: Any) -> Any:
        if self.gateway.close_count:
            raise RuntimeError("model gateway was closed while the runtime still referenced it")
        return input


class _UsageTrackingGateway(_ClosableGateway):
    def build_agent_model(self, *, tools: list[Any] | None = None) -> _UsageTrackingModel:
        return _UsageTrackingModel(self)


def test_runtime_cache_ignores_execution_identity(monkeypatch) -> None:
    gateway = _Gateway()
    compiled_graph = object()
    compile_calls: list[dict[str, Any]] = []

    def build_graph(**kwargs: Any) -> object:
        compile_calls.append(kwargs)
        return compiled_graph

    monkeypatch.setattr("agent.runtime.container.build_agent_graph", build_graph)
    container = RuntimeContainer(
        settings=get_settings(),
        model_gateway=gateway,
        checkpointer=object(),
    )

    first = container.create_runtime(
        model_config_id="model-config-v1",
        tool_permissions=("remember",),
        user_id="user-a",
        agent_id="agent-a",
        session_id="thread-a",
    )
    second = container.create_runtime(
        model_config_id="model-config-v1",
        tool_permissions=("remember",),
        user_id="user-b",
        agent_id="agent-b",
        session_id="thread-b",
    )

    assert first.graph is second.graph
    assert first.graph._value is compiled_graph
    assert gateway.build_count == 1
    assert len(compile_calls) == 1
    assert "store" not in compile_calls[0]
    assert first.config["configurable"]["agent_id"] == "agent-a"
    assert second.config["configurable"]["agent_id"] == "agent-b"


def test_runtime_model_and_graph_caches_are_lru_bounded(monkeypatch) -> None:
    settings = get_settings().model_copy(deep=True)
    settings.agent.runtime_cache_capacity = 2
    monkeypatch.setattr(
        "agent.runtime.container.build_agent_graph",
        lambda **kwargs: object(),
    )
    container = RuntimeContainer(
        settings=settings,
        model_gateway=_Gateway(),
        checkpointer=object(),
    )

    first_permissions = ("remember",)
    second_permissions = ("recall_memory",)
    third_permissions = ("current_datetime",)
    container.graph_for_permissions("model-config-v1", first_permissions)
    container.graph_for_permissions("model-config-v1", second_permissions)
    container.graph_for_permissions("model-config-v1", first_permissions)
    container.graph_for_permissions("model-config-v1", third_permissions)

    second_key = container._runtime_cache_key(
        model_config_id="model-config-v1",
        tools=container.get_tools(second_permissions),
        tool_permissions=second_permissions,
    )
    first_key = container._runtime_cache_key(
        model_config_id="model-config-v1",
        tools=container.get_tools(first_permissions),
        tool_permissions=first_permissions,
    )
    entry = container._runtime_entries["model-config-v1"]
    assert len(entry.models) == 2
    assert len(entry.graphs) == 2
    assert first_key not in entry.models
    assert second_key not in entry.graphs


def test_runtime_cache_isolates_model_configuration_versions(monkeypatch) -> None:
    gateway = _Gateway()
    monkeypatch.setattr(
        "agent.runtime.container.build_agent_graph",
        lambda **kwargs: object(),
    )
    container = RuntimeContainer(
        settings=get_settings(),
        model_gateway=gateway,
        checkpointer=object(),
    )

    first = container.create_runtime(
        model_config_id="model-config-v1",
        tool_permissions=("remember",),
        user_id="user-a",
        agent_id="agent-a",
        session_id="thread-a",
    )
    second = container.create_runtime(
        model_config_id="model-config-v2",
        tool_permissions=("remember",),
        user_id="user-a",
        agent_id="agent-a",
        session_id="thread-b",
    )

    assert first.graph is not second.graph
    assert gateway.build_count == 2


def test_runtime_cache_single_flights_concurrent_same_key_builds(monkeypatch) -> None:
    class SlowGateway(_Gateway):
        def build_agent_model(self, *, tools: list[Any] | None = None) -> _Model:
            time.sleep(0.03)
            return super().build_agent_model(tools=tools)

    gateway = SlowGateway()
    compile_count = 0
    compile_lock = threading.Lock()

    def build_graph(**_kwargs: Any) -> object:
        nonlocal compile_count
        time.sleep(0.03)
        with compile_lock:
            compile_count += 1
        return object()

    monkeypatch.setattr("agent.runtime.container.build_agent_graph", build_graph)
    container = RuntimeContainer(
        settings=get_settings(),
        model_gateway=gateway,
        checkpointer=object(),
    )
    barrier = threading.Barrier(8)

    def build(index: int) -> object:
        barrier.wait()
        return container.create_runtime(
            model_config_id="model-config-v1",
            tool_permissions=("remember",),
            user_id=f"user-{index}",
            agent_id="agent",
            session_id=f"thread-{index}",
        ).graph

    with ThreadPoolExecutor(max_workers=8) as executor:
        graphs = list(executor.map(build, range(8)))

    assert len({id(graph) for graph in graphs}) == 1
    assert gateway.build_count == 1
    assert compile_count == 1


def test_gateway_cache_single_flights_concurrent_same_configuration(
    monkeypatch,
) -> None:
    calls = 0
    calls_lock = threading.Lock()

    def get_runtime_by_id(_service, _session, model_config_id: str):
        nonlocal calls
        time.sleep(0.03)
        with calls_lock:
            calls += 1
        return SimpleNamespace(
            id=model_config_id,
            base_url="https://models.example.test/v1",
            api_key=SecretStr("test-secret"),
            model_name="test-model",
        )

    monkeypatch.setattr(
        "agent.runtime.container.ModelConfigurationService.get_runtime_by_id",
        get_runtime_by_id,
    )
    container = RuntimeContainer(settings=get_settings(), checkpointer=object())
    barrier = threading.Barrier(8)

    def build(_index: int):
        barrier.wait()
        return container.gateway_for_model_config("model-config-v1")

    with ThreadPoolExecutor(max_workers=8) as executor:
        gateways = list(executor.map(build, range(8)))

    assert len({id(gateway) for gateway in gateways}) == 1
    assert calls == 1


def test_gateway_cache_closes_retired_gateways_after_last_handle_once(monkeypatch) -> None:
    settings = get_settings().model_copy(deep=True)
    settings.agent.runtime_cache_capacity = 1
    created: dict[str, _ClosableGateway] = {}

    def get_runtime_by_id(_service, _session, model_config_id: str):
        return SimpleNamespace(
            id=model_config_id,
            base_url="https://models.example.test/v1",
            api_key=SecretStr("test-secret"),
            model_name="test-model",
        )

    def build_gateway(**kwargs: Any) -> _ClosableGateway:
        gateway = _ClosableGateway(**kwargs)
        created[gateway.model_config_id] = gateway
        return gateway

    monkeypatch.setattr(
        "agent.runtime.container.ModelConfigurationService.get_runtime_by_id",
        get_runtime_by_id,
    )
    monkeypatch.setattr("agent.runtime.container.ModelGateway", build_gateway)
    container = RuntimeContainer(settings=settings, checkpointer=object())

    first = container.gateway_for_model_config("model-config-v1")
    second = container.gateway_for_model_config("model-config-v2")

    assert created["model-config-v1"].close_count == 0
    assert created["model-config-v2"].close_count == 0

    del first
    gc.collect()

    assert created["model-config-v1"].close_count == 1

    container.close()
    container.close()

    assert created["model-config-v1"].close_count == 1
    assert created["model-config-v2"].close_count == 0

    del second
    gc.collect()

    assert created["model-config-v2"].close_count == 1


def test_retired_owner_registry_shrinks_after_last_handle_closes_gateway(
    monkeypatch,
) -> None:
    settings = get_settings().model_copy(deep=True)
    settings.agent.runtime_cache_capacity = 1
    created: dict[str, _ClosableGateway] = {}

    def build_gateway(**kwargs: Any) -> _ClosableGateway:
        gateway = _ClosableGateway(**kwargs)
        created[gateway.model_config_id] = gateway
        return gateway

    monkeypatch.setattr(
        "agent.runtime.container.ModelConfigurationService.get_runtime_by_id",
        lambda _service, _session, model_config_id: SimpleNamespace(
            id=model_config_id,
            base_url="https://models.example.test/v1",
            api_key=SecretStr("test-secret"),
            model_name="test-model",
        ),
    )
    monkeypatch.setattr("agent.runtime.container.ModelGateway", build_gateway)
    container = RuntimeContainer(settings=settings, checkpointer=object())

    first = container.gateway_for_model_config("model-config-v1")
    container.gateway_for_model_config("model-config-v2")

    assert len(container._retired_owners) == 1

    del first
    gc.collect()

    assert created["model-config-v1"].close_count == 1
    assert len(container._retired_owners) == 0


def test_retired_owner_registry_stays_bounded_across_repeated_evictions(
    monkeypatch,
) -> None:
    settings = get_settings().model_copy(deep=True)
    settings.agent.runtime_cache_capacity = 1

    monkeypatch.setattr(
        "agent.runtime.container.ModelConfigurationService.get_runtime_by_id",
        lambda _service, _session, model_config_id: SimpleNamespace(
            id=model_config_id,
            base_url="https://models.example.test/v1",
            api_key=SecretStr("test-secret"),
            model_name="test-model",
        ),
    )
    monkeypatch.setattr("agent.runtime.container.ModelGateway", _ClosableGateway)
    container = RuntimeContainer(settings=settings, checkpointer=object())
    current = container.gateway_for_model_config("model-config-v0")

    for index in range(1, 21):
        next_gateway = container.gateway_for_model_config(f"model-config-v{index}")
        assert len(container._retired_owners) == 1

        del current
        gc.collect()

        assert len(container._retired_owners) == 0
        current = next_gateway
        del next_gateway

    del current
    container.close()

    assert len(container._retired_owners) == 0


def test_running_loop_evictions_close_and_discard_retired_owners(monkeypatch) -> None:
    class _SuspendingAsyncCloseGateway(_ClosableGateway):
        def __init__(self, *, model_config_id: str, **kwargs: Any) -> None:
            super().__init__(model_config_id=model_config_id, **kwargs)
            self.async_close_count = 0
            self.close_started = asyncio.Event()
            self.allow_close = asyncio.Event()
            self.async_close_finished = asyncio.Event()

        def close(self) -> bool:
            self.close_count += 1
            return False

        async def aclose(self) -> None:
            self.async_close_count += 1
            self.close_started.set()
            await self.allow_close.wait()
            self.async_close_finished.set()

    settings = get_settings().model_copy(deep=True)
    settings.agent.runtime_cache_capacity = 1
    created: dict[str, _SuspendingAsyncCloseGateway] = {}

    def build_gateway(**kwargs: Any) -> _SuspendingAsyncCloseGateway:
        gateway = _SuspendingAsyncCloseGateway(**kwargs)
        created[gateway.model_config_id] = gateway
        return gateway

    monkeypatch.setattr(
        "agent.runtime.container.ModelConfigurationService.get_runtime_by_id",
        lambda _service, _session, model_config_id: SimpleNamespace(
            id=model_config_id,
            base_url="https://models.example.test/v1",
            api_key=SecretStr("test-secret"),
            model_name="test-model",
        ),
    )
    monkeypatch.setattr("agent.runtime.container.ModelGateway", build_gateway)
    container = RuntimeContainer(settings=settings, checkpointer=object())

    async def exercise() -> None:
        current = container.gateway_for_model_config("model-config-v0")

        for index in range(1, 4):
            next_gateway = container.gateway_for_model_config(f"model-config-v{index}")
            retired_gateway = created[f"model-config-v{index - 1}"]

            assert len(container._retired_owners) == 1

            del current
            gc.collect()
            await asyncio.sleep(0)

            assert retired_gateway.close_started.is_set()
            assert len(container._retired_owners) == 1

            retired_gateway.allow_close.set()
            await retired_gateway.async_close_finished.wait()

            assert len(container._retired_owners) == 0
            current = next_gateway
            del next_gateway

        current_gateway = created["model-config-v3"]
        del current
        gc.collect()

        shutdown = asyncio.create_task(container.aclose())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert current_gateway.close_started.is_set()
        assert len(container._retired_owners) == 1
        assert not shutdown.done()

        current_gateway.allow_close.set()
        await shutdown

        assert current_gateway.async_close_count == 1
        assert len(container._retired_owners) == 0

    asyncio.run(exercise())

    assert all(gateway.async_close_count == 1 for gateway in created.values())


def test_running_loop_close_monitor_failure_is_retryable_without_task_error() -> None:
    async def exercise() -> None:
        gateway = _FlakyAsyncClosableGateway(model_config_id="model-config-v1")
        owner = _GatewayOwner(gateway, close_when_retired=True)
        loop = asyncio.get_running_loop()
        task_errors: list[dict[str, Any]] = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(
            lambda _loop, context: task_errors.append(context),
        )
        try:
            assert owner.retire() is False
            await asyncio.sleep(0)

            assert gateway.async_close_attempts == 1
            assert owner.closed is False
            assert owner._close_complete.is_set()
            assert task_errors == []

            await owner.aclose()

            assert owner.closed is True
            assert gateway.async_close_attempts == 2
            assert task_errors == []
        finally:
            loop.set_exception_handler(previous_handler)

    asyncio.run(exercise())


def test_running_loop_close_monitor_cancellation_is_retryable() -> None:
    class _CancellableGateway(_ClosableGateway):
        def __init__(self, *, model_config_id: str) -> None:
            super().__init__(model_config_id=model_config_id)
            self.async_close_attempts = 0
            self.close_started = asyncio.Event()

        def close(self) -> bool:
            return False

        async def aclose(self) -> None:
            self.async_close_attempts += 1
            if self.async_close_attempts == 1:
                self.close_started.set()
                await asyncio.Event().wait()

    async def exercise() -> None:
        gateway = _CancellableGateway(model_config_id="model-config-v1")
        owner = _GatewayOwner(gateway, close_when_retired=True)

        assert owner.retire() is False
        await asyncio.sleep(0)

        assert gateway.close_started.is_set()
        with owner._lock:
            monitor = owner._close_monitor
        assert monitor is not None

        monitor.cancel()
        await monitor

        assert owner.closed is False
        assert owner._close_complete.is_set()

        await owner.aclose()

        assert owner.closed is True
        assert gateway.async_close_attempts == 2

    asyncio.run(exercise())


def test_running_loop_monitor_cancelled_before_start_is_retryable() -> None:
    class _ImmediateAsyncCloseGateway(_ClosableGateway):
        def __init__(self, *, model_config_id: str) -> None:
            super().__init__(model_config_id=model_config_id)
            self.async_close_attempts = 0

        def close(self) -> bool:
            return False

        async def aclose(self) -> None:
            self.async_close_attempts += 1

    async def exercise() -> None:
        gateway = _ImmediateAsyncCloseGateway(model_config_id="model-config-v1")
        owner = _GatewayOwner(gateway, close_when_retired=True)

        assert owner.retire() is False
        with owner._lock:
            monitor = owner._close_monitor
        assert monitor is not None

        monitor.cancel()
        with pytest.raises(asyncio.CancelledError):
            await monitor

        assert owner.closed is False
        assert owner._close_complete.is_set()

        await owner.aclose()

        assert owner.closed is True
        assert gateway.async_close_attempts == 1

    asyncio.run(exercise())


def test_gateway_owner_eager_monitor_immediate_success_settles_owner() -> None:
    class _ImmediateSuccessGateway(_ClosableGateway):
        def __init__(self, *, model_config_id: str) -> None:
            super().__init__(model_config_id=model_config_id)
            self.async_close_attempts = 0

        def close(self) -> bool:
            return False

        async def aclose(self) -> None:
            self.async_close_attempts += 1

    async def exercise() -> None:
        gateway = _ImmediateSuccessGateway(model_config_id="model-config-v1")
        closed_owners: list[_GatewayOwner] = []
        owner = _GatewayOwner(
            gateway,
            close_when_retired=True,
            on_closed=closed_owners.append,
        )
        owner._lock = threading.RLock()
        loop = asyncio.get_running_loop()
        previous_factory = loop.get_task_factory()
        loop.set_task_factory(asyncio.eager_task_factory)
        try:
            assert owner.retire() is False

            assert owner.closed is True
            assert owner._close_complete.is_set()
            assert closed_owners == [owner]
            assert gateway.async_close_attempts == 1
            with owner._lock:
                assert owner._close_monitor is None
        finally:
            loop.set_task_factory(previous_factory)

    asyncio.run(exercise())


def test_gateway_owner_eager_monitor_immediate_failure_is_retryable() -> None:
    class _ImmediateFailureGateway(_ClosableGateway):
        def __init__(self, *, model_config_id: str) -> None:
            super().__init__(model_config_id=model_config_id)
            self.async_close_attempts = 0

        def close(self) -> bool:
            return False

        async def aclose(self) -> None:
            self.async_close_attempts += 1
            if self.async_close_attempts == 1:
                raise RuntimeError("eager close failed")

    async def exercise() -> None:
        gateway = _ImmediateFailureGateway(model_config_id="model-config-v1")
        owner = _GatewayOwner(gateway, close_when_retired=True)
        owner._lock = threading.RLock()
        loop = asyncio.get_running_loop()
        task_errors: list[dict[str, Any]] = []
        previous_factory = loop.get_task_factory()
        previous_handler = loop.get_exception_handler()
        loop.set_task_factory(asyncio.eager_task_factory)
        loop.set_exception_handler(
            lambda _loop, context: task_errors.append(context),
        )
        try:
            assert owner.retire() is False

            assert owner.closed is False
            assert owner._close_complete.is_set()
            assert gateway.async_close_attempts == 1
            assert task_errors == []
            with owner._lock:
                assert owner._state == "active"
                assert owner._close_monitor is None

            await owner.aclose()

            assert owner.closed is True
            assert gateway.async_close_attempts == 2
            assert task_errors == []
        finally:
            loop.set_exception_handler(previous_handler)
            loop.set_task_factory(previous_factory)

    asyncio.run(exercise())


def test_gateway_owner_task_factory_failure_closes_coroutine_and_is_retryable() -> None:
    class _FactoryFailureGateway(_ClosableGateway):
        def __init__(self, *, model_config_id: str) -> None:
            super().__init__(model_config_id=model_config_id)
            self.async_close_attempts = 0

        def close(self) -> bool:
            return False

        async def aclose(self) -> None:
            self.async_close_attempts += 1

    async def exercise() -> None:
        gateway = _FactoryFailureGateway(model_config_id="model-config-v1")
        owner = _GatewayOwner(gateway, close_when_retired=True)
        loop = asyncio.get_running_loop()
        previous_factory = loop.get_task_factory()
        factory_lock_states: list[bool] = []
        scheduled_coroutines: list[Any] = []

        def raising_task_factory(
            _loop: asyncio.AbstractEventLoop,
            coroutine: Any,
            **_kwargs: Any,
        ) -> asyncio.Future[Any]:
            factory_lock_states.append(owner._lock.locked())
            scheduled_coroutines.append(coroutine)
            raise RuntimeError("task factory failed")

        loop.set_task_factory(raising_task_factory)
        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always", RuntimeWarning)
            try:
                with pytest.raises(RuntimeError, match="task factory failed"):
                    owner.close()
            finally:
                loop.set_task_factory(previous_factory)

            coroutine_closed = [
                coroutine.cr_frame is None for coroutine in scheduled_coroutines
            ]
            scheduled_coroutines.clear()
            gc.collect()

        assert factory_lock_states == [False]
        assert coroutine_closed == [True]
        assert not any(
            "was never awaited" in str(captured.message)
            for captured in caught_warnings
        )
        assert owner.closed is False
        assert owner._close_complete.is_set()
        with owner._lock:
            assert owner._state == "active"
            assert owner._close_monitor is None

        await owner.aclose()

        assert owner.closed is True
        assert gateway.async_close_attempts == 1

    asyncio.run(exercise())


def test_async_container_shutdown_retries_failed_evicted_gateway_close(monkeypatch) -> None:
    settings = get_settings().model_copy(deep=True)
    settings.agent.runtime_cache_capacity = 1
    created: dict[str, _FlakyClosableGateway] = {}

    def build_gateway(**kwargs: Any) -> _FlakyClosableGateway:
        gateway = _FlakyClosableGateway(**kwargs)
        created[gateway.model_config_id] = gateway
        return gateway

    monkeypatch.setattr(
        "agent.runtime.container.ModelConfigurationService.get_runtime_by_id",
        lambda _service, _session, model_config_id: SimpleNamespace(
            id=model_config_id,
            base_url="https://models.example.test/v1",
            api_key=SecretStr("test-secret"),
            model_name="test-model",
        ),
    )
    monkeypatch.setattr("agent.runtime.container.ModelGateway", build_gateway)
    container = RuntimeContainer(settings=settings, checkpointer=object())

    first = container.gateway_for_model_config("model-config-v1")
    container.gateway_for_model_config("model-config-v2")
    del first
    gc.collect()

    assert created["model-config-v1"].close_attempts == 1
    asyncio.run(container.aclose())
    assert created["model-config-v1"].close_attempts == 2


def test_explicit_container_close_propagates_failure_and_remains_retryable(
    monkeypatch,
) -> None:
    created: dict[str, _FlakyClosableGateway] = {}

    def build_gateway(**kwargs: Any) -> _FlakyClosableGateway:
        gateway = _FlakyClosableGateway(**kwargs)
        created[gateway.model_config_id] = gateway
        return gateway

    monkeypatch.setattr(
        "agent.runtime.container.ModelConfigurationService.get_runtime_by_id",
        lambda _service, _session, model_config_id: SimpleNamespace(
            id=model_config_id,
            base_url="https://models.example.test/v1",
            api_key=SecretStr("test-secret"),
            model_name="test-model",
        ),
    )
    monkeypatch.setattr("agent.runtime.container.ModelGateway", build_gateway)
    container = RuntimeContainer(settings=get_settings(), checkpointer=object())
    container.gateway_for_model_config("model-config-v1")

    with pytest.raises(RuntimeError, match="first close failed"):
        container.close()

    assert created["model-config-v1"].close_attempts == 1
    container.close()
    assert created["model-config-v1"].close_attempts == 2


def test_gateway_owner_async_close_propagates_failure_and_remains_retryable() -> None:
    gateway = _FlakyAsyncClosableGateway(model_config_id="model-config-v1")
    owner = _GatewayOwner(gateway, close_when_retired=True)

    assert owner.retire() is False
    with pytest.raises(RuntimeError, match="first async close failed"):
        asyncio.run(owner.aclose())

    assert owner.closed is False
    asyncio.run(owner.aclose())

    assert owner.closed is True
    assert gateway.async_close_attempts == 2


def test_gateway_owner_async_close_can_retry_after_cancellation() -> None:
    class CancellableGateway(_ClosableGateway):
        def __init__(self, *, model_config_id: str) -> None:
            super().__init__(model_config_id=model_config_id)
            self.async_close_attempts = 0
            self.close_started = asyncio.Event()

        def close(self) -> bool:
            return False

        async def aclose(self) -> None:
            self.async_close_attempts += 1
            if self.async_close_attempts == 1:
                self.close_started.set()
                await asyncio.Event().wait()

    gateway = CancellableGateway(model_config_id="model-config-v1")
    owner = _GatewayOwner(gateway, close_when_retired=True)

    assert owner.retire() is False

    async def exercise() -> None:
        close_task = asyncio.create_task(owner.aclose())
        await gateway.close_started.wait()
        close_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close_task

        try:
            await asyncio.wait_for(owner.aclose(), timeout=0.2)
        finally:
            owner._close_complete.set()

        assert owner.closed is True
        assert gateway.async_close_attempts == 2

    asyncio.run(exercise())


def test_runtime_reference_keeps_gateway_alive_after_capacity_eviction(monkeypatch) -> None:
    settings = get_settings().model_copy(deep=True)
    settings.agent.runtime_cache_capacity = 1
    created: dict[str, _UsageTrackingGateway] = {}

    def get_runtime_by_id(_service, _session, model_config_id: str):
        return SimpleNamespace(
            id=model_config_id,
            base_url="https://models.example.test/v1",
            api_key=SecretStr("test-secret"),
            model_name="test-model",
        )

    def build_gateway(**kwargs: Any) -> _UsageTrackingGateway:
        gateway = _UsageTrackingGateway(**kwargs)
        created[gateway.model_config_id] = gateway
        return gateway

    monkeypatch.setattr(
        "agent.runtime.container.ModelConfigurationService.get_runtime_by_id",
        get_runtime_by_id,
    )
    monkeypatch.setattr("agent.runtime.container.ModelGateway", build_gateway)
    monkeypatch.setattr(
        "agent.runtime.container.build_agent_graph",
        lambda **kwargs: kwargs["model"],
    )
    container = RuntimeContainer(settings=settings, checkpointer=object())

    first = container.create_runtime(
        model_config_id="model-config-v1",
        user_id="user-1",
        agent_id="agent-1",
        session_id="session-1",
    )
    container.create_runtime(
        model_config_id="model-config-v2",
        user_id="user-2",
        agent_id="agent-2",
        session_id="session-2",
    )

    assert created["model-config-v1"].close_count == 0
    assert first.graph.invoke("still usable") == "still usable"

    del first
    gc.collect()

    assert created["model-config-v1"].close_count == 1


def test_in_flight_graph_keeps_gateway_alive_during_capacity_eviction(monkeypatch) -> None:
    settings = get_settings().model_copy(deep=True)
    settings.agent.runtime_cache_capacity = 1
    invocation_started = threading.Event()
    release_invocation = threading.Event()
    created: dict[str, _UsageTrackingGateway] = {}

    class BlockingModel(_UsageTrackingModel):
        def invoke(self, input: Any) -> Any:
            invocation_started.set()
            assert release_invocation.wait(timeout=2.0)
            return super().invoke(input)

    class BlockingGateway(_UsageTrackingGateway):
        def build_agent_model(self, *, tools: list[Any] | None = None) -> BlockingModel:
            return BlockingModel(self)

    def get_runtime_by_id(_service, _session, model_config_id: str):
        return SimpleNamespace(
            id=model_config_id,
            base_url="https://models.example.test/v1",
            api_key=SecretStr("test-secret"),
            model_name="test-model",
        )

    def build_gateway(**kwargs: Any) -> BlockingGateway:
        gateway = BlockingGateway(**kwargs)
        created[gateway.model_config_id] = gateway
        return gateway

    monkeypatch.setattr(
        "agent.runtime.container.ModelConfigurationService.get_runtime_by_id",
        get_runtime_by_id,
    )
    monkeypatch.setattr("agent.runtime.container.ModelGateway", build_gateway)
    monkeypatch.setattr(
        "agent.runtime.container.build_agent_graph",
        lambda **kwargs: kwargs["model"],
    )
    container = RuntimeContainer(settings=settings, checkpointer=object())
    first_graph = container.create_runtime(
        model_config_id="model-config-v1",
        user_id="user-1",
        agent_id="agent-1",
        session_id="session-1",
    ).graph

    with ThreadPoolExecutor(max_workers=1) as executor:
        invocation = executor.submit(first_graph.invoke, "completed")
        assert invocation_started.wait(timeout=2.0)
        container.create_runtime(
            model_config_id="model-config-v2",
            user_id="user-2",
            agent_id="agent-2",
            session_id="session-2",
        )
        assert created["model-config-v1"].close_count == 0
        release_invocation.set()
        assert invocation.result(timeout=2.0) == "completed"

    del first_graph
    gc.collect()

    assert created["model-config-v1"].close_count == 1


def test_tool_registry_exposes_complete_execution_contract() -> None:
    container = RuntimeContainer(
        settings=get_settings(),
        model_gateway=_Gateway(),
        checkpointer=object(),
    )

    descriptions = container.tool_registry.describe()

    assert descriptions
    assert all(
        {
            "name",
            "version",
            "input_schema",
            "max_output_chars",
            "timeout_seconds",
                "permission",
                "confirmation_policy",
                "execution_mode",
        }
        <= description.keys()
        for description in descriptions
    )
    timeout_by_name = {
        description["name"]: description["timeout_seconds"] for description in descriptions
    }
    assert timeout_by_name["fetch_hotspots"] == 180.0
    assert timeout_by_name["prepare_topic_research"] == 180.0
    assert all(
        timeout == 60.0
        for name, timeout in timeout_by_name.items()
        if name not in {"fetch_hotspots", "prepare_topic_research"}
    )
    mode_by_name = {
        description["name"]: description["execution_mode"] for description in descriptions
    }
    assert mode_by_name["prepare_topic_research"] == "cooperative"


def test_explicit_empty_tool_permissions_resolve_to_no_tools() -> None:
    container = RuntimeContainer(
        settings=get_settings(),
        model_gateway=_Gateway(),
        checkpointer=object(),
    )

    assert container.get_tools(None)
    assert container.get_tools(("*",))
    assert container.get_tools(()) == []
