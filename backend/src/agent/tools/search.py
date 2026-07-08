from __future__ import annotations

from agent.prompts.registry import load_tool_description
from agent.runtime.context import get_tool_runtime_context
from integrations.search import search_integration
from langchain_core.tools import tool


@tool("search_topic_sources", description=load_tool_description("search_topic_sources"))
def search_topic(topic: str) -> dict:
    """检索已确认内容选题的资料、事实、信源和背景信息。"""
    if not get_tool_runtime_context().can_use_tool("search_topic_sources"):
        return {"error": "Tool is not allowed for this run.", "tool": "search_topic_sources"}
    return search_integration.search_topic_sources(topic)
