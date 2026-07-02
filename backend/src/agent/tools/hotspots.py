from __future__ import annotations

from integrations.hotspots import fetch_hotspot_sources
from langchain_core.tools import tool


def _parse_csv(value: str | None) -> list[str] | None:
    if not value or value.strip().lower() == "all":
        return None
    return [item.strip().lower() for item in value.split(",") if item.strip()]


@tool("fetch_hotspots")
def fetch_hotspots(source: str = "all", platforms: str = "all") -> dict:
    """Fetch current hot topics when the user asks for trends, hot searches, or热点.

    source accepts all, rss, tikhub, aihot, or comma-separated values.
    platforms accepts all or comma-separated TikHub platforms:
    douyin, bilibili, xiaohongshu, weibo.
    API keys are read from server environment variables and are never supplied
    by the model. Each platform returns at most 10 items.
    """
    return fetch_hotspot_sources(
        sources=_parse_csv(source),
        tikhub_platforms=_parse_csv(platforms),
    )

