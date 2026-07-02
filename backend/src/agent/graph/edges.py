from __future__ import annotations

from agent.graph.state import AgentState
from langchain_core.messages import AIMessage


def route_after_agent(state: AgentState) -> str:
    messages = state.get("messages", [])
    iterations = int(state.get("iterations", 0))
    max_iterations = int(state.get("max_iterations", 8))
    last_message = messages[-1] if messages else None

    if (
        isinstance(last_message, AIMessage)
        and last_message.tool_calls
        and iterations < max_iterations
    ):
        return "tools"
    return "end"
