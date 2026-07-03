from __future__ import annotations

from typing import Final

HOTSPOT_SOURCE_OPTIONS: Final[dict[str, str]] = {
    "36kr": "36Kr",
    "huxiu": "虎嗅",
    "ifanr": "爱范儿",
    "douyin": "抖音",
    "bilibili": "Bilibili",
    "xiaohongshu": "小红书",
    "weibo": "微博",
    "aihot": "AI HOT",
}

RSS_HOTSPOT_SOURCES: Final[tuple[str, ...]] = ("36kr", "huxiu", "ifanr")
TIKHUB_HOTSPOT_SOURCES: Final[tuple[str, ...]] = (
    "douyin",
    "bilibili",
    "xiaohongshu",
    "weibo",
)
AIHOT_SOURCE: Final[str] = "aihot"
DEFAULT_HOTSPOT_SOURCES: Final[tuple[str, ...]] = tuple(HOTSPOT_SOURCE_OPTIONS)


def normalize_hotspot_sources(values: list[str] | tuple[str, ...]) -> list[str]:
    selected: list[str] = []
    for value in values:
        source = str(value).strip().lower()
        if source not in HOTSPOT_SOURCE_OPTIONS:
            raise ValueError(f"Unsupported hotspot source: {value}")
        if source not in selected:
            selected.append(source)
    if not selected:
        raise ValueError("hotspot_sources cannot be empty")
    return selected


def render_hotspot_sources(values: list[str] | tuple[str, ...]) -> str:
    sources = normalize_hotspot_sources(values)
    return "\n".join(
        f"- {source}: {HOTSPOT_SOURCE_OPTIONS[source]}" for source in sources
    )


def split_hotspot_sources(
    values: list[str] | tuple[str, ...],
) -> tuple[list[str], list[str], list[str]]:
    sources = normalize_hotspot_sources(values)
    rss_sources = [source for source in sources if source in RSS_HOTSPOT_SOURCES]
    tikhub_platforms = [source for source in sources if source in TIKHUB_HOTSPOT_SOURCES]
    source_groups: list[str] = []
    if rss_sources:
        source_groups.append("rss")
    if tikhub_platforms:
        source_groups.append("tikhub")
    if AIHOT_SOURCE in sources:
        source_groups.append("aihot")
    return source_groups, rss_sources, tikhub_platforms
