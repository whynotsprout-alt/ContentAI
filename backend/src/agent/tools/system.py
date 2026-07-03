from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from agent.prompts.registry import load_tool_description
from langchain_core.tools import tool


@tool("current_datetime", description=load_tool_description("current_datetime"))
def current_datetime(timezone: str = "Asia/Shanghai") -> dict[str, str]:
    """获取当前日期和时间。"""
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = UTC
        timezone = "UTC"
    now = datetime.now(tz)
    return {
        "timezone": timezone,
        "iso": now.isoformat(),
        "date": now.date().isoformat(),
        "time": now.strftime("%H:%M:%S"),
    }
