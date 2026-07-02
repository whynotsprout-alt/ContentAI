from __future__ import annotations

from integrations.search import search_topic_sources
from langchain_core.tools import tool


@tool("search_topic_sources")
def search_topic(topic: str) -> dict:
    """Search a confirmed content topic with Metaso and Anspire.

    Use this after the user has chosen or confirmed a topic and needs source
    material, facts, references, or background. API keys are read from server
    environment variables. Each provider returns at most 10 results.
    """
    return search_topic_sources(topic)

