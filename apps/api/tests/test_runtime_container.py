from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

from agent.runtime.container import RuntimeContainer
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

    assert first.graph is second.graph is compiled_graph
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
    assert len(container._model_cache) == 2
    assert len(container._graph_cache) == 2
    assert first_key not in container._model_cache
    assert second_key not in container._graph_cache


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
