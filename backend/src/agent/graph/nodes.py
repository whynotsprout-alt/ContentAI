from __future__ import annotations

from typing import Any

from agent.graph.state import AgentState
from agent.runtime.events import emit_event
from langchain_core.messages import AIMessage, SystemMessage
from langgraph.prebuilt import ToolNode


def build_agent_node(model: Any):
    def agent_node(state: AgentState) -> dict[str, Any]:
        emit_event("agent_node", {"status": "started"})
        messages = state.get("messages", [])
        system_prompt = state.get("system_prompt", "")
        model_input = (
            [SystemMessage(content=system_prompt), *messages]
            if system_prompt
            else messages
        )
        response = model.invoke(model_input)
        if not isinstance(response, AIMessage):
            raise RuntimeError("LLM did not return an AIMessage.")
        emit_event("agent_node", {"status": "finished"})
        return {
            "messages": [response],
            "iterations": int(state.get("iterations", 0)) + 1,
        }

    return agent_node


def build_tools_node(tools: list[Any]):
    tool_node = ToolNode(tools)

    def tools_node(state: AgentState) -> dict[str, Any]:
        emit_event("tools_node", {"status": "started"})
        result = tool_node.invoke(state)
        emit_event("tools_node", {"status": "finished"})
        return result

    return tools_node
