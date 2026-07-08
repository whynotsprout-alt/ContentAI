from __future__ import annotations

from agent.prompts.registry import load_tool_description
from agent.runtime.context import get_tool_runtime_context
from langchain_core.tools import tool


@tool("remember", description=load_tool_description("remember"))
def remember(content: str, kind: str = "semantic") -> dict[str, str]:
    """保存用户明确要求长期记住的信息。"""
    context = get_tool_runtime_context()
    if not context.can_use_tool("remember"):
        return {"error": "Tool is not allowed for this run.", "tool": "remember"}
    try:
        entry = context.long_term_memory.remember(
            context.account_id,
            content,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            session_id=context.session_id,
            kind=kind or "semantic",
            payload={"source": "tool", "execution_id": context.execution_id},
            source_type="tool",
            source_execution_id=context.execution_id,
        )
    except ValueError as exc:
        return {"error": str(exc), "tool": "remember"}
    return {
        "key": entry.key,
        "kind": entry.kind,
        "content": entry.content,
    }


@tool("recall_memory", description=load_tool_description("recall_memory"))
def recall_memory(query: str) -> dict[str, list[dict[str, str]]]:
    """召回与当前问题相关的长期记忆。"""
    context = get_tool_runtime_context()
    if not context.can_use_tool("recall_memory"):
        return {"error": "Tool is not allowed for this run.", "tool": "recall_memory"}
    memories = context.long_term_memory.recall(
        context.account_id,
        query,
        tenant_id=context.tenant_id,
        user_id=context.user_id,
        limit=8,
    )
    return {
        "memories": [
            {"key": memory.key, "kind": memory.kind, "content": memory.content}
            for memory in memories
        ]
    }
