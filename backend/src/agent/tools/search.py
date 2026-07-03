from __future__ import annotations

from agent.prompts.registry import load_tool_description
from integrations.search import search_topic_sources
from langchain_core.tools import tool


@tool("search_topic_sources", description=load_tool_description("search_topic_sources"))
def search_topic(topic: str) -> dict:
    """检索已确认内容选题的资料、事实、信源和背景信息。"""
    return search_topic_sources(topic)
