from __future__ import annotations

from agent.tools.hotspots import fetch_hotspots
from agent.tools.memory import recall_memory, remember
from agent.tools.search import search_topic
from agent.tools.system import current_datetime

TOOL_SET = [remember, recall_memory, current_datetime, fetch_hotspots, search_topic]


def build_tool_set() -> list[object]:
    return list(TOOL_SET)


def tool_names(tools: list[object]) -> list[str]:
    return [str(getattr(tool, "name", tool)) for tool in tools]
