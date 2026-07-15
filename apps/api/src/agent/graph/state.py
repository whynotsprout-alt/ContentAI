from __future__ import annotations

from typing import Annotated, Literal, TypedDict

from agent.runtime.context import SharedRuntimeContext
from langchain_core.messages import BaseMessage
from langgraph.graph import add_messages

TaskStatus = Literal["idle", "thinking", "executing", "waiting", "completed", "error"]


class AgentRuntimeContext(SharedRuntimeContext):
    """Runtime context carried outside checkpointed state."""


class AgentGraphConfig(TypedDict, total=False):
    execution_id: str
    thread_id: str
    session_id: str
    conversation_id: str
    user_id: str
    agent_id: str
    tenant_id: str
    permissions: list[str]
    api_keys: dict[str, str]


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
