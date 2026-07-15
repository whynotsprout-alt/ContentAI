from __future__ import annotations

import json

from agent.prompts.registry import load_tool_description
from agent.runtime.context import get_tool_runtime_context
from langchain_core.tools import tool
from memory.long_term import is_sensitive_memory
from models.enums import MemoryKind, MemorySourceType

_ALLOWED_MEMORY_KINDS: tuple[str, ...] = tuple(sorted({kind.value for kind in MemoryKind}))
_MAX_REMEMBER_CONTENT_LENGTH = 1000
_MEMORY_PREVIEW_LENGTH = 1800
_MEMORY_TEXT_FIELDS = (
    "summary",
    "text",
    "content",
    "message",
    "result",
    "answer",
    "output",
    "note",
    "notes",
)


@tool("remember", description=load_tool_description("remember"))
def remember(content: str, kind: str = "semantic") -> dict[str, str]:
    """Persist runtime memory with content/ kind restrictions."""
    context = get_tool_runtime_context()
    if context.long_term_memory is None:
        return {
            "error": "Tool runtime context is incomplete for memory storage.",
            "tool": "remember",
        }
    if not context.can_use_tool("remember"):
        return {"error": "Tool is not allowed for this run.", "tool": "remember"}

    normalized_content = _normalize_text(_extract_memory_text(content))
    if not normalized_content:
        return {"error": "Content is required for memory storage.", "tool": "remember"}
    normalized_kind = _normalize_kind(kind)
    sanitized_content = normalized_content[:_MAX_REMEMBER_CONTENT_LENGTH]
    if is_sensitive_memory(sanitized_content):
        return {
            "error": "Potentially sensitive content is not allowed for memory storage.",
            "tool": "remember",
        }

    try:
        entry = context.long_term_memory.remember(
            context.agent_id,
            sanitized_content,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            session_id=context.conversation_id,
            kind=normalized_kind,
            payload={
                **_memory_scope_metadata(context),
                "source": "tool",
                "execution_id": context.execution_id,
            },
            source_type=MemorySourceType.tool,
            source_execution_id=context.execution_id,
        )
    except ValueError as exc:
        return {"error": str(exc), "tool": "remember"}
    return {
        "key": entry.key,
        "kind": entry.kind,
        "content": entry.content[:_MEMORY_PREVIEW_LENGTH],
        "tool": "remember",
    }


@tool("recall_memory", description=load_tool_description("recall_memory"))
def recall_memory(query: str) -> dict[str, list[dict[str, str]]]:
    """Return scoped memory for current context."""
    context = get_tool_runtime_context()
    if context.long_term_memory is None:
        return {
            "error": "Tool runtime context is incomplete for memory lookup.",
            "tool": "recall_memory",
        }
    if not context.can_use_tool("recall_memory"):
        return {"error": "Tool is not allowed for this run.", "tool": "recall_memory"}

    normalized_query = _normalize_text(query)
    if not normalized_query:
        return {"memories": []}

    memories = context.long_term_memory.recall(
        context.agent_id,
        normalized_query,
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


def _normalize_text(value: str) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _extract_memory_text(value: str) -> str:
    if value is None:
        return ""
    raw = str(value)
    candidate = _normalize_text(raw)
    if not candidate:
        return ""
    parsed_candidate = _safe_parse_json(raw)
    if parsed_candidate is not None:
        extracted = _extract_text_from_payload(parsed_candidate)
        if extracted:
            return extracted
    return candidate


def _safe_parse_json(value: str) -> object | None:
    try:
        return json.loads(value)
    except Exception:
        return None


def _extract_text_from_payload(value: object) -> str:
    if isinstance(value, dict):
        for field in _MEMORY_TEXT_FIELDS:
            if field in value and isinstance(value[field], str):
                normalized = _normalize_text(value[field])
                if normalized:
                    return normalized
        for item in value.values():
            if isinstance(item, str):
                normalized = _normalize_text(item)
                if normalized:
                    return normalized
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                normalized = _normalize_text(item)
                if normalized:
                    return normalized
            extracted = _extract_text_from_payload(item)
            if extracted:
                return extracted
    return ""


def _memory_scope_metadata(context) -> dict[str, object]:
    return {
        "scope": {
            "tenant_id": context.tenant_id,
            "user_id": context.user_id,
            "agent_id": context.agent_id,
            "session_id": context.conversation_id,
            "thread_id": context.session_id,
            "execution_id": context.execution_id,
        }
    }


def _normalize_kind(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    return normalized if normalized in _ALLOWED_MEMORY_KINDS else MemoryKind.semantic
