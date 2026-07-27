from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from agent.context.assembler import AgentContext, ContextAssembler
from agent.context.window import TokenCounter
from langchain_core.messages import BaseMessage, HumanMessage, messages_from_dict, messages_to_dict
from memory import LongTermMemory, MemoryRepository, ShortTermMemory
from memory.long_term import is_sensitive_memory, is_transient_task_memory
from memory.types import MemoryEntry

TURN_CONTEXT_SNAPSHOT_VERSION = 1
MAX_TURN_CONTEXT_SNAPSHOT_BYTES = 262_144
MAX_TURN_CONTEXT_SNAPSHOT_MESSAGES = 48
MAX_TURN_CONTEXT_SNAPSHOT_MEMORIES = 8


class TurnContextSnapshotError(ValueError):
    """A durable execution payload is absent, malformed, or outside its bounded contract."""


@dataclass(frozen=True)
class DurableTurnContext:
    system_prompt: str
    messages: list[BaseMessage]
    focus_message: str
    role: str
    tool_permissions: tuple[str, ...]


@dataclass(frozen=True)
class TurnPromptInputs:
    messages: list[BaseMessage]
    short_term_summary: str
    long_term_memories: list[MemoryEntry]


def normalize_tool_permissions(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list | tuple):
        raise TurnContextSnapshotError
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise TurnContextSnapshotError
        permission = value.strip()
        if not permission or len(permission) > 120:
            raise TurnContextSnapshotError
        normalized.append(permission)
    return tuple(sorted(set(normalized)))


def build_turn_context_snapshot(
    *,
    context: AgentContext,
    prompt_inputs: TurnPromptInputs,
    tool_permissions: tuple[str, ...] | list[str],
    role: str,
    execution_id: str,
    invocation_id: str,
    session_id: str,
    user_id: str,
    agent_id: str,
    agent_version_id: str,
    message_id: str,
    focus_message: str,
) -> dict[str, Any]:
    """Create the bounded, canonical prompt/auth payload persisted with an execute outbox row."""
    normalized_permissions = normalize_tool_permissions(tool_permissions)
    context_messages = messages_to_dict(context.messages)
    if len(context_messages) > MAX_TURN_CONTEXT_SNAPSHOT_MESSAGES:
        raise TurnContextSnapshotError
    body: dict[str, Any] = {
        "version": TURN_CONTEXT_SNAPSHOT_VERSION,
        "lineage": {
            "execution_id": execution_id,
            "invocation_id": invocation_id,
            "session_id": session_id,
            "user_id": user_id,
            "agent_id": agent_id,
            "agent_version_id": agent_version_id,
            "message_id": message_id,
        },
        "auth": {
            "role": _bounded_text(role, max_chars=40),
            "tool_permissions": list(normalized_permissions),
        },
        "turn_context": {
            "focus_message": _bounded_text(focus_message, max_chars=16_000),
            "system_prompt": _bounded_text(context.system_prompt, max_chars=64_000),
            "messages": context_messages,
            "short_term_summary": _bounded_text(
                prompt_inputs.short_term_summary, max_chars=64_000
            ),
            "long_term_memories": _snapshot_memories(prompt_inputs.long_term_memories),
        },
    }
    _validate_lineage(body["lineage"])
    encoded = _canonical_snapshot_json(body)
    if len(encoded) > MAX_TURN_CONTEXT_SNAPSHOT_BYTES:
        raise TurnContextSnapshotError
    return {
        **body,
        "digest": hashlib.sha256(encoded).hexdigest(),
    }


def load_turn_context_snapshot(
    payload: Any,
    *,
    execution_id: str,
    invocation_id: str,
    session_id: str,
    user_id: str,
    agent_id: str,
    agent_version_id: str,
    message_id: str,
) -> DurableTurnContext:
    """Validate an execute outbox snapshot and reconstruct only its model input."""
    if not isinstance(payload, dict) or set(payload) != {
        "version",
        "lineage",
        "auth",
        "turn_context",
        "digest",
    }:
        raise TurnContextSnapshotError
    if payload.get("version") != TURN_CONTEXT_SNAPSHOT_VERSION:
        raise TurnContextSnapshotError
    digest = payload.get("digest")
    if not isinstance(digest, str) or len(digest) != 64:
        raise TurnContextSnapshotError
    body = {key: payload[key] for key in ("version", "lineage", "auth", "turn_context")}
    encoded = _canonical_snapshot_json(body)
    if len(encoded) > MAX_TURN_CONTEXT_SNAPSHOT_BYTES or not hmac.compare_digest(
        digest, hashlib.sha256(encoded).hexdigest()
    ):
        raise TurnContextSnapshotError

    lineage = body["lineage"]
    _validate_lineage(lineage)
    expected_lineage = {
        "execution_id": execution_id,
        "invocation_id": invocation_id,
        "session_id": session_id,
        "user_id": user_id,
        "agent_id": agent_id,
        "agent_version_id": agent_version_id,
        "message_id": message_id,
    }
    if lineage != expected_lineage:
        raise TurnContextSnapshotError

    auth = body["auth"]
    if not isinstance(auth, dict) or set(auth) != {"role", "tool_permissions"}:
        raise TurnContextSnapshotError
    role = auth.get("role")
    permissions = auth.get("tool_permissions")
    if not isinstance(role, str) or not role.strip() or len(role) > 40:
        raise TurnContextSnapshotError
    normalized_permissions = normalize_tool_permissions(permissions)
    if permissions != list(normalized_permissions):
        raise TurnContextSnapshotError

    turn_context = body["turn_context"]
    if not isinstance(turn_context, dict) or set(turn_context) != {
        "focus_message",
        "system_prompt",
        "messages",
        "short_term_summary",
        "long_term_memories",
    }:
        raise TurnContextSnapshotError
    focus_message = _validated_text(turn_context.get("focus_message"), max_chars=16_000)
    system_prompt = _validated_text(turn_context.get("system_prompt"), max_chars=64_000)
    _validated_text(turn_context.get("short_term_summary"), max_chars=64_000)
    raw_messages = turn_context.get("messages")
    if (
        not isinstance(raw_messages, list)
        or not raw_messages
        or len(raw_messages) > MAX_TURN_CONTEXT_SNAPSHOT_MESSAGES
        or not all(isinstance(message, dict) for message in raw_messages)
    ):
        raise TurnContextSnapshotError
    memories = turn_context.get("long_term_memories")
    if not isinstance(memories, list) or len(memories) > MAX_TURN_CONTEXT_SNAPSHOT_MEMORIES:
        raise TurnContextSnapshotError
    if not all(_is_valid_snapshot_memory(memory) for memory in memories):
        raise TurnContextSnapshotError
    try:
        messages = messages_from_dict(raw_messages)
    except Exception as exc:  # noqa: BLE001
        raise TurnContextSnapshotError from exc
    if not all(isinstance(message, BaseMessage) for message in messages):
        raise TurnContextSnapshotError
    return DurableTurnContext(
        system_prompt=system_prompt,
        messages=messages,
        focus_message=focus_message,
        role=role,
        tool_permissions=normalized_permissions,
    )


def fallback_turn_context(*, content: str) -> tuple[TurnPromptInputs, AgentContext]:
    """Keep non-runtime unit tests durable without widening the production execution path."""
    message = HumanMessage(content=content)
    prompt_inputs = TurnPromptInputs(
        messages=[message], short_term_summary="", long_term_memories=[]
    )
    return prompt_inputs, AgentContext(
        system_prompt="",
        messages=[message],
        short_term_summary="",
        long_term_memories=[],
    )


def _snapshot_memories(memories: list[MemoryEntry]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []
    for memory in memories:
        kind = str(memory.kind or "").strip()
        content = str(memory.content or "").strip()
        if (
            not kind
            or len(kind) > 40
            or not content
            or len(content) > 1_000
            or kind.lower() in {"instruction", "system", "system_prompt", "guardrail", "policy"}
            or is_sensitive_memory(content)
            or is_transient_task_memory(content)
        ):
            continue
        snapshots.append(
            {
                "key": _bounded_text(memory.key, max_chars=160),
                "kind": kind,
                "content": content,
                "confidence": float(memory.confidence),
                "importance_score": float(memory.importance_score),
            }
        )
        if len(snapshots) >= MAX_TURN_CONTEXT_SNAPSHOT_MEMORIES:
            break
    return snapshots


def _is_valid_snapshot_memory(memory: Any) -> bool:
    if not isinstance(memory, dict) or set(memory) != {
        "key",
        "kind",
        "content",
        "confidence",
        "importance_score",
    }:
        return False
    try:
        return (
            bool(_validated_text(memory.get("key"), max_chars=160))
            and bool(_validated_text(memory.get("kind"), max_chars=40))
            and bool(_validated_text(memory.get("content"), max_chars=1_000))
            and not is_sensitive_memory(memory["content"])
            and not is_transient_task_memory(memory["content"])
            and isinstance(memory.get("confidence"), int | float)
            and isinstance(memory.get("importance_score"), int | float)
        )
    except TurnContextSnapshotError:
        return False


def _validate_lineage(lineage: Any) -> None:
    required = {
        "execution_id",
        "invocation_id",
        "session_id",
        "user_id",
        "agent_id",
        "agent_version_id",
        "message_id",
    }
    if not isinstance(lineage, dict) or set(lineage) != required:
        raise TurnContextSnapshotError
    for value in lineage.values():
        if not isinstance(value, str) or not value.strip() or len(value) > 160:
            raise TurnContextSnapshotError


def _bounded_text(value: Any, *, max_chars: int) -> str:
    if not isinstance(value, str) or len(value) > max_chars:
        raise TurnContextSnapshotError
    return value


def _validated_text(value: Any, *, max_chars: int) -> str:
    return _bounded_text(value, max_chars=max_chars)


def _canonical_snapshot_json(payload: dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TurnContextSnapshotError from exc


def load_turn_prompt_inputs(
    db_session: Any,
    *,
    session_id: str,
    user_id: str,
    agent_id: str,
    focus_message: str,
    pending_message: BaseMessage | None = None,
) -> TurnPromptInputs:
    """Read the same non-mutating prompt inputs before and after turn persistence."""
    repository = MemoryRepository(db_session)
    short_term = ShortTermMemory(repository)
    summary, messages = short_term.load(
        db_session,
        session_id=session_id,
        user_id=user_id,
        refresh_if_missing=False,
        touch=False,
    )
    if pending_message is not None:
        messages = [*messages, pending_message]
    recalled = LongTermMemory(repository).recall(
        agent_id,
        focus_message,
        user_id=user_id,
        limit=8,
        touch=False,
    )
    return TurnPromptInputs(
        messages=messages,
        short_term_summary=summary,
        long_term_memories=recalled,
    )


def assemble_turn_context(
    *,
    context_assembler: ContextAssembler,
    context_window_tokens: int,
    chat_max_tokens: int,
    agent_profile: Any,
    agent_version: Any,
    prompt_inputs: TurnPromptInputs,
    tool_names: list[str],
    focus_message: str,
    user_id: str,
    conversation_id: str,
    execution_id: str,
    research_package: Any | None,
    token_counter: TokenCounter | None,
) -> AgentContext:
    """Use one assembly path for preflight and the worker's real model input."""
    return context_assembler.assemble(
        context_window_tokens=context_window_tokens,
        chat_max_tokens=chat_max_tokens,
        agent_profile=agent_profile,
        agent_version=agent_version,
        messages=prompt_inputs.messages,
        short_term_summary=prompt_inputs.short_term_summary,
        long_term_memories=prompt_inputs.long_term_memories,
        tool_names=tool_names,
        focus_message=focus_message,
        user_id=user_id,
        conversation_id=conversation_id,
        run_id=execution_id,
        permissions=tool_names,
        research_package=research_package,
        token_counter=token_counter,
    )


__all__ = [
    "DurableTurnContext",
    "MAX_TURN_CONTEXT_SNAPSHOT_BYTES",
    "TurnContextSnapshotError",
    "TurnPromptInputs",
    "assemble_turn_context",
    "build_turn_context_snapshot",
    "fallback_turn_context",
    "load_turn_context_snapshot",
    "load_turn_prompt_inputs",
    "normalize_tool_permissions",
]
