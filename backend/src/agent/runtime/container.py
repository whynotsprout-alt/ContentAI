from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.context.assembler import ContextAssembler
from agent.graph.factory import build_agent_graph
from agent.infrastructure.llm import ModelGateway
from memory.execution_state import ExecutionStateManager
from agent.runtime.checkpoint import build_checkpointer, build_store
from memory.message_persister import MessagePersister
from agent.tools.registry import build_tool_set
from core.config import Settings, get_settings


@dataclass
class RuntimeContainer:
    settings: Settings = field(default_factory=get_settings)
    model_gateway: Any | None = None
    tools: list[Any] = field(init=False)
    checkpointer: Any = field(init=False)
    store: Any = field(init=False)
    model: Any = field(init=False)
    graph: Any = field(init=False)
    assembler: ContextAssembler = field(default_factory=ContextAssembler)
    state_manager: ExecutionStateManager = field(default_factory=ExecutionStateManager)
    message_persister: MessagePersister = field(default_factory=MessagePersister)

    def __post_init__(self) -> None:
        gateway = self.model_gateway or ModelGateway(self.settings)
        self.model_gateway = gateway
        self.tools = build_tool_set()
        self.checkpointer = build_checkpointer()
        self.store = build_store()
        self.model = gateway.build_agent_model(tools=self.tools)
        self.graph = build_agent_graph(
            model=self.model,
            tools=self.tools,
            checkpointer=self.checkpointer,
            store=self.store,
        )


_runtime_container: RuntimeContainer | None = None


def get_runtime_container(settings: Settings | None = None) -> RuntimeContainer:
    global _runtime_container
    if _runtime_container is None:
        _runtime_container = RuntimeContainer(settings=settings or get_settings())
    return _runtime_container


def set_runtime_container(container: RuntimeContainer) -> None:
    global _runtime_container
    _runtime_container = container


def reset_runtime_container() -> None:
    global _runtime_container
    _runtime_container = None
