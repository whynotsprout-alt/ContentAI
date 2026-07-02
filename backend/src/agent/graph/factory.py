from __future__ import annotations

from typing import Any

from agent.graph.edges import route_after_agent
from agent.graph.nodes import build_agent_node, build_tools_node
from agent.graph.state import AgentState
from langgraph.graph import END, START, StateGraph


def build_agent_graph(
    *,
    model: Any,
    tools: list[Any],
    checkpointer: Any,
    store: Any,
) -> Any:
    graph = StateGraph(AgentState)
    graph.add_node("agent", build_agent_node(model))
    graph.add_node("tools", build_tools_node(tools))
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_after_agent, {"tools": "tools", "end": END})
    graph.add_edge("tools", "agent")
    return graph.compile(checkpointer=checkpointer, store=store)
