from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from langchain_core.tools import tool


@tool("current_datetime")
def current_datetime(timezone: str = "Asia/Shanghai") -> dict[str, str]:
    """Return the current date and time for scheduling or date-sensitive replies."""
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
