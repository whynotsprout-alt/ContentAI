from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from memory import LongTermMemory


@dataclass
class ToolRuntimeContext:
    execution_id: str
    session_id: str
    account_id: str
    allowed_hotspot_sources: list[str]
    long_term_memory: LongTermMemory
    tenant_id: str = "local"
    user_id: str = "local-user"
    tool_permissions: tuple[str, ...] = ("*",)

    def can_use_tool(self, tool_name: str) -> bool:
        return "*" in self.tool_permissions or tool_name in self.tool_permissions


_runtime_context: ContextVar[ToolRuntimeContext | None] = ContextVar(
    "tool_runtime_context",
    default=None,
)


def get_tool_runtime_context() -> ToolRuntimeContext:
    context = _runtime_context.get()
    if context is None:
        raise RuntimeError("Tool runtime context is not initialized.")
    return context


@contextmanager
def tool_runtime_scope(context: ToolRuntimeContext):
    token = _runtime_context.set(context)
    try:
        yield
    finally:
        _runtime_context.reset(token)
