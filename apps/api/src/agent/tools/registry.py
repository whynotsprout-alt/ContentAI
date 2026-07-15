from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from agent.tools.hotspots import fetch_hotspots
from agent.tools.memory import recall_memory, remember
from agent.tools.research import prepare_topic_research
from agent.tools.system import current_datetime

TOOL_SET = [
    remember,
    recall_memory,
    current_datetime,
    fetch_hotspots,
    prepare_topic_research,
]

# Fetching hotspots fans out to several RSS and platform APIs, then runs one
# or more scoring batches. It legitimately takes longer than small local
# tools, so it needs a dedicated limit rather than inheriting the 60-second
# default used by simple tools.
TOOL_TIMEOUT_OVERRIDES: dict[str, float] = {"fetch_hotspots": 180.0}


def _normalize_permissions(permissions: Sequence[str] | None) -> tuple[str, ...]:
    if permissions is None:
        return ("*",)
    if not permissions:
        return ()
    if any(permission == "*" for permission in permissions):
        return ("*",)
    deduped: list[str] = []
    for permission in permissions:
        permission_name = str(permission).strip()
        if permission_name and permission_name not in deduped:
            deduped.append(permission_name)
    return tuple(deduped)


def _tool_name(tool: object) -> str:
    return str(getattr(tool, "name", tool))


def build_tool_set() -> list[Any]:
    return list(TOOL_SET)


def tool_names(tools: Sequence[Any]) -> list[str]:
    return [_tool_name(tool) for tool in tools]


class ToolConfirmationPolicy(StrEnum):
    never = "never"
    configured = "configured"
    always = "always"


@dataclass(frozen=True)
class ToolRegistration:
    tool: Any
    name: str
    version: str
    input_schema: dict[str, Any]
    max_output_chars: int
    timeout_seconds: float
    permission: str
    confirmation_policy: ToolConfirmationPolicy
    side_effecting: bool
    execution_mode: str


@dataclass
class ToolRegistry:
    tools: list[Any] = field(default_factory=lambda: list(TOOL_SET))
    default_timeout_seconds: float = 60.0
    default_max_output_chars: int = 240_000

    @property
    def registrations(self) -> tuple[ToolRegistration, ...]:
        return tuple(self._registration(tool) for tool in self.tools)

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": registration.name,
                "version": registration.version,
                "input_schema": registration.input_schema,
                "max_output_chars": registration.max_output_chars,
                "timeout_seconds": registration.timeout_seconds,
                "permission": registration.permission,
                "confirmation_policy": registration.confirmation_policy.value,
                "side_effecting": registration.side_effecting,
                "execution_mode": registration.execution_mode,
            }
            for registration in self.registrations
        ]

    def get_tools(self, permissions: Sequence[str] | None = None) -> list[Any]:
        normalized_permissions = _normalize_permissions(permissions)
        if normalized_permissions == ("*",):
            return list(self.tools)
        return [tool for tool in self.tools if _tool_name(tool) in set(normalized_permissions)]

    def resolve_tools(self, permissions: Sequence[str] | None = None) -> list[Any]:
        return self.get_tools(permissions)

    def _registration(self, tool: Any) -> ToolRegistration:
        metadata = getattr(tool, "metadata", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        name = _tool_name(tool)
        args_schema = getattr(tool, "args_schema", None)
        schema_builder = getattr(args_schema, "model_json_schema", None)
        input_schema = schema_builder() if callable(schema_builder) else {}
        return ToolRegistration(
            tool=tool,
            name=name,
            version=str(metadata.get("version") or "1"),
            input_schema=input_schema,
            max_output_chars=max(
                1,
                int(metadata.get("max_output_chars") or self.default_max_output_chars),
            ),
            timeout_seconds=max(
                0.001,
                float(
                    metadata.get("timeout_seconds")
                    or TOOL_TIMEOUT_OVERRIDES.get(name, self.default_timeout_seconds)
                ),
            ),
            permission=str(metadata.get("permission") or name),
            confirmation_policy=ToolConfirmationPolicy(
                metadata.get("confirmation_policy") or ToolConfirmationPolicy.configured
            ),
            side_effecting=bool(metadata.get("side_effecting", name == "remember")),
            execution_mode=(
                "cooperative" if metadata.get("execution_mode") == "cooperative" else "threaded"
            ),
        )


__all__ = [
    "ToolConfirmationPolicy",
    "ToolRegistration",
    "ToolRegistry",
    "build_tool_set",
    "tool_names",
]
