from __future__ import annotations

from langchain_core.messages import BaseMessage

MAX_CONTEXT_MESSAGES = 40


def trim_context_window(
    messages: list[BaseMessage],
    *,
    limit: int = MAX_CONTEXT_MESSAGES,
) -> list[BaseMessage]:
    return messages[-limit:]
