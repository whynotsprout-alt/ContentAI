from __future__ import annotations

from typing import Any, Literal

from contentai.agent.graph.state import AgentState
from langchain_core.messages import AIMessage

RouteAfterAgent = Literal["tools", "human", "tool_error", "end"]
RouteAfterTools = Literal["agent", "tool_error"]
RouteAfterHuman = Literal["tools", "human", "end"]


def route_after_agent(state: AgentState) -> RouteAfterAgent:
    if state.get("tool_error"):
        return "tool_error"

    messages = state.get("messages", [])
    if not messages:
        return "end"
    last_message = messages[-1]
    if not isinstance(last_message, AIMessage):
        return "end"

    tool_calls = list(getattr(last_message, "tool_calls", []) or [])
    if not tool_calls:
        return "end"
    if _has_invalid_tool_call(tool_calls, state):
        return "tool_error"
    if _requires_confirmation(tool_calls, state) and state.get("human_approved") is not True:
        return "human"
    return "tools"


def route_after_tools(state: AgentState) -> RouteAfterTools:
    return "tool_error" if state.get("tool_error") else "agent"


def route_after_human(state: AgentState) -> RouteAfterHuman:
    approved = state.get("human_approved")
    if approved is True:
        return "tools"
    if approved is False:
        return "end"
    return "human"


def _requires_confirmation(tool_calls: list[Any], state: AgentState) -> bool:
    configured = state.get("confirmation_tool_names", [])
    if not isinstance(configured, list):
        return False
    required = {str(name).strip() for name in configured if str(name).strip()}
    if not required:
        return False
    if "*" in required:
        return True
    return any((_extract_tool_name(call) or "") in required for call in tool_calls)


def _has_invalid_tool_call(tool_calls: list[Any], state: AgentState) -> bool:
    available_tools = state.get("available_tool_names", [])
    if not isinstance(available_tools, list):
        return False
    valid = {name for name in available_tools if isinstance(name, str) and name}
    if "*" in valid:
        return False
    return any((_extract_tool_name(call) or "") not in valid for call in tool_calls)


def _extract_tool_name(tool_call: Any) -> str | None:
    name = (
        tool_call.get("name") if isinstance(tool_call, dict) else getattr(tool_call, "name", None)
    )
    return name.strip() if isinstance(name, str) and name.strip() else None
