from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages

TaskStatus = Literal["idle", "thinking", "executing", "waiting", "completed", "error"]


class AgentStateCore(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    task_status: TaskStatus


class AgentStateOptional(TypedDict, total=False):
    available_tool_names: list[str]
    confirmation_tool_names: list[str]
    human_approved: bool | None
    tool_error: str | dict[str, str] | None
    tool_error_count: int


class AgentState(AgentStateCore, AgentStateOptional):
    """Minimal checkpointed state for the ReAct graph."""
