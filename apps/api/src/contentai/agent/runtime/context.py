from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, TypedDict

from contentai.memory import LongTermMemory


class SharedRuntimeContext(TypedDict):
    execution_id: str
    session_id: str
    conversation_id: str
    user_id: str
    agent_id: str
    permissions: list[str]
    api_keys: dict[str, str]


def _normalize_permissions(permissions: Iterable[str] | None) -> list[str]:
    if permissions is None:
        return ["*"]
    normalized: list[str] = []
    for permission in permissions:
        permission_value = str(permission).strip()
        if not permission_value:
            continue
        if permission_value == "*":
            return ["*"]
        if permission_value not in normalized:
            normalized.append(permission_value)
    if not normalized:
        return ["*"]
    return normalized


def _normalize_api_keys(values: Mapping[str, str] | None) -> dict[str, str]:
    if not values:
        return {}
    normalized: dict[str, str] = {}
    for key, value in values.items():
        normalized[str(key)] = str(value or "").strip()
    return normalized


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(k): v for k, v in value.items()}


def _coerce_str_list(value: Iterable[str] | None) -> list[str]:
    if value is None:
        return []
    normalized: list[str] = []
    for item in value:
        item_value = str(item).strip()
        if item_value and item_value not in normalized:
            normalized.append(item_value)
    return normalized


@dataclass
class ToolRuntimeContext:
    execution_id: str
    conversation_id: str
    session_id: str
    agent_id: str
    user_id: str
    agent_version_id: str = ""
    permissions: list[str] = field(default_factory=lambda: ["*"])
    api_keys: dict[str, str] = field(default_factory=dict)
    allowed_hotspot_sources: list[str] = field(default_factory=list)
    topic_scoring_prompt: str = ""
    hotspot_filter_model: Any | None = None
    research_model_gateway: Any | None = None
    model_usage_callback_factory: Callable[[str], Iterable[Any]] | None = None
    event_writer: Any | None = None
    tool_policies: dict[str, dict[str, Any]] = field(default_factory=dict)
    long_term_memory: LongTermMemory | None = None
    cancellation_check: Callable[[], None] | None = None
    side_effect_dispatcher: Callable[[dict[str, Any]], None] | None = None
    side_effect_receipt_poller: Callable[[str, str], dict[str, Any] | None] | None = None

    def __post_init__(self) -> None:
        self.permissions = list(_normalize_permissions(self.permissions))
        self.api_keys = _normalize_api_keys(_coerce_mapping(self.api_keys))
        self.allowed_hotspot_sources = _coerce_str_list(self.allowed_hotspot_sources)
        self.topic_scoring_prompt = str(self.topic_scoring_prompt or "").strip()
        self.tool_policies = {
            str(name): dict(policy)
            for name, policy in self.tool_policies.items()
            if isinstance(policy, Mapping)
        }

    @property
    def tool_permissions(self) -> tuple[str, ...]:
        return tuple(self.permissions)

    def can_use_tool(self, tool_name: str) -> bool:
        return "*" in self.permissions or tool_name in self.permissions

    def ensure_not_cancelled(self) -> None:
        if self.cancellation_check is not None:
            self.cancellation_check()

    def model_usage_callbacks(self, category: str) -> list[Any]:
        """Build callbacks for an isolated internal model call."""
        factory = self.model_usage_callback_factory
        if factory is None:
            return []
        return list(factory(str(category).strip()) or [])

    def as_runtime_context(self) -> SharedRuntimeContext:
        return {
            "execution_id": self.execution_id,
            "session_id": self.session_id,
            "conversation_id": self.conversation_id,
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "permissions": list(self.permissions),
            "api_keys": dict(self.api_keys),
        }

    def as_graph_configurable(self) -> dict[str, Any]:
        return dict(self.as_runtime_context())


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
