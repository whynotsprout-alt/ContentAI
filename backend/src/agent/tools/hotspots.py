from __future__ import annotations

from agent.prompts.registry import load_tool_description
from agent.runtime.context import get_tool_runtime_context
from core.hotspot_sources import (
    AIHOT_SOURCE,
    RSS_HOTSPOT_SOURCES,
    TIKHUB_HOTSPOT_SOURCES,
    normalize_hotspot_sources,
    split_hotspot_sources,
)
from integrations.hotspots import fetch_hotspot_sources
from langchain_core.tools import tool


def _parse_csv(value: str | None) -> list[str] | None:
    if not value or value.strip().lower() == "all":
        return None
    return [item.strip().lower() for item in value.split(",") if item.strip()]


@tool("fetch_hotspots", description=load_tool_description("fetch_hotspots"))
def fetch_hotspots(source: str = "all", platforms: str = "all", rss_sources: str = "all") -> dict:
    """获取当前热点、热榜、热搜或趋势话题，结果会被当前账号允许的来源限制。"""
    runtime = get_tool_runtime_context()
    allowed_sources = normalize_hotspot_sources(runtime.allowed_hotspot_sources)
    selected_sources = _select_requested_sources(
        allowed_sources=allowed_sources,
        source_groups=_parse_csv(source),
        tikhub_platforms=_parse_csv(platforms),
        rss_sources=_parse_csv(rss_sources),
    )
    if not selected_sources:
        return {
            "items": [],
            "sources": {},
            "errors": [
                {
                    "source": "fetch_hotspots",
                    "error": "Requested hotspot sources are not enabled for this account.",
                    "allowed_hotspot_sources": allowed_sources,
                }
            ],
            "allowed_hotspot_sources": allowed_sources,
            "selected_hotspot_sources": [],
        }

    source_groups, selected_rss_sources, selected_tikhub_platforms = split_hotspot_sources(
        selected_sources
    )
    result = fetch_hotspot_sources(
        sources=source_groups,
        rss_sources=selected_rss_sources,
        tikhub_platforms=selected_tikhub_platforms,
    )
    result["allowed_hotspot_sources"] = allowed_sources
    result["selected_hotspot_sources"] = selected_sources
    return result


def _select_requested_sources(
    *,
    allowed_sources: list[str],
    source_groups: list[str] | None,
    tikhub_platforms: list[str] | None,
    rss_sources: list[str] | None,
) -> list[str]:
    requested: list[str] = []
    groups = source_groups or ["rss", "tikhub", "aihot"]

    if "rss" in groups:
        requested.extend(rss_sources or list(RSS_HOTSPOT_SOURCES))
    if "tikhub" in groups:
        requested.extend(tikhub_platforms or list(TIKHUB_HOTSPOT_SOURCES))
    if "aihot" in groups:
        requested.append(AIHOT_SOURCE)

    if not requested:
        return []
    try:
        normalized = normalize_hotspot_sources(requested)
    except ValueError:
        return []
    return [source for source in normalized if source in allowed_sources]
