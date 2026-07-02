from __future__ import annotations

from agent.runtime.context import get_tool_runtime_context
from langchain_core.tools import tool


@tool("remember")
def remember(content: str, kind: str = "semantic") -> dict[str, str]:
    """Persist a user preference, profile fact, goal, project detail, or instruction."""
    context = get_tool_runtime_context()
    entry = context.long_term_memory.remember(
        context.account_id,
        content,
        kind=kind or "semantic",
        payload={"source": "tool", "run_id": context.run_id},
    )
    return {
        "key": entry.key,
        "kind": entry.kind,
        "content": entry.content,
    }


@tool("recall_memory")
def recall_memory(query: str) -> dict[str, list[dict[str, str]]]:
    """Recall long-term memories relevant to the current user query."""
    context = get_tool_runtime_context()
    memories = context.long_term_memory.recall(context.account_id, query, limit=8)
    return {
        "memories": [
            {"key": memory.key, "kind": memory.kind, "content": memory.content}
            for memory in memories
        ]
    }
