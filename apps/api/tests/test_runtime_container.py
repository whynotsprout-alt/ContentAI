from __future__ import annotations

from typing import Any

from agent.runtime.container import RuntimeContainer
from core.config import get_settings


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
        tool_permissions=("remember",),
        user_id="user-a",
        agent_id="agent-a",
        tenant_id="tenant-a",
        session_id="thread-a",
    )
    second = container.create_runtime(
        tool_permissions=("remember",),
        user_id="user-b",
        agent_id="agent-b",
        tenant_id="tenant-b",
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
    container.graph_for_permissions(first_permissions)
    container.graph_for_permissions(second_permissions)
    container.graph_for_permissions(first_permissions)
    container.graph_for_permissions(third_permissions)

    second_key = container._runtime_cache_key(
        tools=container.get_tools(second_permissions),
        tool_permissions=second_permissions,
    )
    first_key = container._runtime_cache_key(
        tools=container.get_tools(first_permissions),
        tool_permissions=first_permissions,
    )
    assert len(container._model_cache) == 2
    assert len(container._graph_cache) == 2
    assert first_key not in container._model_cache
    assert second_key not in container._graph_cache


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
