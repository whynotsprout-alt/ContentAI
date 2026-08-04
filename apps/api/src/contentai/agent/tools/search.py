from __future__ import annotations

from langchain_core.tools import tool

from contentai.agent.runtime.context import get_tool_runtime_context
from contentai.integrations.search import search_integration

SEARCH_STAGE_TIMEOUT_SECONDS = 30.0


@tool(
    "search_metaso_sources",
    description="Search the fixed Metaso API for sources about one confirmed topic.",
)
async def search_metaso_sources(topic: str, size: int = 10) -> dict[str, object]:
    runtime = get_tool_runtime_context()
    runtime.ensure_not_cancelled()
    if not runtime.can_use_tool("search_metaso_sources"):
        return {"error": "Tool is not allowed for this run.", "tool": "search_metaso_sources"}
    result = await search_integration.asearch_metaso_sources(
        topic,
        size=size,
        timeout=SEARCH_STAGE_TIMEOUT_SECONDS,
    )
    runtime.ensure_not_cancelled()
    return result


@tool(
    "search_anspire_sources",
    description="Search the fixed Anspire API for sources about one confirmed topic.",
)
async def search_anspire_sources(topic: str, size: int = 10) -> dict[str, object]:
    runtime = get_tool_runtime_context()
    runtime.ensure_not_cancelled()
    if not runtime.can_use_tool("search_anspire_sources"):
        return {"error": "Tool is not allowed for this run.", "tool": "search_anspire_sources"}
    result = await search_integration.asearch_anspire_sources(
        topic,
        size=size,
        timeout=SEARCH_STAGE_TIMEOUT_SECONDS,
    )
    runtime.ensure_not_cancelled()
    return result


__all__ = ["search_anspire_sources", "search_metaso_sources"]
