from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from agent.graph.edges import route_after_agent, route_after_human, route_after_tools
from agent.graph.nodes import (
    build_agent_node,
    build_human_node,
    build_tool_error_node,
    build_tools_node,
)
from agent.graph.state import AgentState
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph


@dataclass(frozen=True)
class AgentGraphBuilder:
    model: Any
    tools: Sequence[BaseTool]
    research_final_model: Any | None = None
    research_presentation_model: Any | None = None
    tool_intent_model: Any | None = None

    def _validate(self) -> None:
        if not callable(getattr(self.model, "invoke", None)):
            raise TypeError(f"model must provide invoke(), got {type(self.model)!r}.")
        if self.tool_intent_model is not None and not callable(
            getattr(self.tool_intent_model, "invoke", None)
        ):
            raise TypeError("tool_intent_model must provide invoke() when configured.")
        for index, tool in enumerate(self.tools):
            if not isinstance(tool, BaseTool):
                raise TypeError(f"tools[{index}] must be a BaseTool instance, got {type(tool)!r}.")

    def build(self) -> StateGraph[AgentState]:
        self._validate()
        graph = StateGraph(AgentState)
        graph.add_node(
            "agent",
            build_agent_node(
                self.model,
                tools=self.tools,
                research_final_model=self.research_final_model,
                research_presentation_model=self.research_presentation_model,
                tool_intent_model=self.tool_intent_model,
            ),
        )
        graph.add_node("tools", build_tools_node(self.tools))
        graph.add_node("human", build_human_node())
        graph.add_node("tool_error", build_tool_error_node())

        graph.add_edge(START, "agent")
        graph.add_conditional_edges(
            "agent",
            route_after_agent,
            {
                "tools": "tools",
                "human": "human",
                "tool_error": "tool_error",
                "end": END,
            },
        )
        graph.add_conditional_edges(
            "tools",
            route_after_tools,
            {"agent": "agent", "tool_error": "tool_error"},
        )
        graph.add_conditional_edges(
            "human",
            route_after_human,
            {"tools": "tools", "human": "human", "end": END},
        )
        graph.add_edge("tool_error", "agent")
        return graph

    def compile(self, **compile_kwargs: Any) -> Any:
        return self.build().compile(**compile_kwargs)


def build_agent_graph(
    *,
    model: Any,
    tools: Sequence[BaseTool],
    research_final_model: Any | None = None,
    research_presentation_model: Any | None = None,
    tool_intent_model: Any | None = None,
    **compile_kwargs: Any,
) -> Any:
    return AgentGraphBuilder(
        model=model,
        tools=tools,
        research_final_model=research_final_model,
        research_presentation_model=research_presentation_model,
        tool_intent_model=tool_intent_model,
    ).compile(**compile_kwargs)
