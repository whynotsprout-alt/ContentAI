from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

MAX_SUMMARY_CHARS = 2400


def summarize_messages(messages: list[BaseMessage], *, max_items: int = 12) -> str:
    lines: list[str] = []
    for message in messages[-max_items:]:
        if isinstance(message, HumanMessage):
            role = "user"
        elif isinstance(message, AIMessage):
            role = "assistant"
        elif isinstance(message, SystemMessage):
            role = "system"
        else:
            continue

        content = str(message.content).strip()
        if content:
            lines.append(f"{role}: {content[:360]}")
    return "\n".join(lines)[-MAX_SUMMARY_CHARS:]
