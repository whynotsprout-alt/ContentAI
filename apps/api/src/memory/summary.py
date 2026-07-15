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


def merge_summaries(previous: str, addition: str) -> str:
    """Keep durable early constraints while continuously incorporating recent turns."""
    previous = previous.strip()
    addition = addition.strip()
    if not previous:
        return addition[:MAX_SUMMARY_CHARS]
    if not addition:
        return previous[:MAX_SUMMARY_CHARS]
    combined = f"{previous}\n{addition}"
    if len(combined) <= MAX_SUMMARY_CHARS:
        return combined
    early_budget = MAX_SUMMARY_CHARS // 2
    recent_budget = MAX_SUMMARY_CHARS - early_budget - 5
    return f"{combined[:early_budget].rstrip()}\n...\n{combined[-recent_budget:].lstrip()}"
