from __future__ import annotations

import hashlib
import json
from typing import Any

from agent.runtime.checkpoint import checkpoint_interrupts
from agent.tools.memory import normalize_remember_input
from memory.long_term import is_sensitive_memory
from models.base import utcnow
from models.chat import ExecutionResumeRequest
from models.schemas.chat import PublicInterrupt, PublicInterruptAction, PublicMemoryProposal
from sqlmodel import Session


def interrupt_identity(payload: dict[str, Any] | None) -> tuple[str, str]:
    interrupts = payload.get("interrupts") if isinstance(payload, dict) else None
    first = interrupts[0] if isinstance(interrupts, list) and interrupts else {}
    interrupt_id = str(first.get("id") or "") if isinstance(first, dict) else ""
    value = first.get("value") if isinstance(first, dict) else None
    tool_calls = value.get("tool_calls") if isinstance(value, dict) else []
    return interrupt_id, stable_json_hash(tool_calls)


def stable_json_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_resume_value(request: ExecutionResumeRequest) -> Any:
    if request.decision in {"approve", "reject"}:
        return {"decision": request.decision}
    if isinstance(request.value, dict) and request.value.get("decision") in {
        "approve",
        "reject",
    }:
        return {"decision": request.value["decision"]}
    return None


def pending_interrupt_ids(
    checkpointer: Any, *, thread_id: str, execution_id: str
) -> set[str]:
    return set(
        pending_interrupt_descriptors(
            checkpointer,
            thread_id=thread_id,
            execution_id=execution_id,
        )
    )


def pending_interrupt_descriptors(
    checkpointer: Any,
    *,
    thread_id: str,
    execution_id: str,
) -> dict[str, str]:
    output: dict[str, str] = {}
    for interrupt in checkpoint_interrupts(
        checkpointer,
        thread_id=thread_id,
        execution_id=execution_id,
    ):
        interrupt_id = (
            interrupt.get("id") if isinstance(interrupt, dict) else getattr(interrupt, "id", None)
        )
        if interrupt_id:
            value = (
                interrupt.get("value")
                if isinstance(interrupt, dict)
                else getattr(interrupt, "value", None)
            )
            tool_calls = value.get("tool_calls") if isinstance(value, dict) else []
            output[str(interrupt_id)] = stable_json_hash(tool_calls)
    return output


def mark_resume_consumed(
    session: Session,
    request_id: str | None,
    *,
    commit: bool = False,
) -> None:
    if not request_id:
        return
    request = session.get(ExecutionResumeRequest, request_id)
    if request is None or request.status == "consumed":
        return
    now = utcnow()
    request.status = "consumed"
    request.consumed_at = now
    request.updated_at = now
    session.add(request)
    if commit:
        session.commit()


def _public_interrupt(payload: dict[str, Any] | None) -> PublicInterrupt | None:
    interrupts = payload.get("interrupts") if isinstance(payload, dict) else None
    first = interrupts[0] if isinstance(interrupts, list) and interrupts else None
    if not isinstance(first, dict):
        return None
    interrupt_id = first.get("id")
    if (
        not isinstance(interrupt_id, str)
        or not interrupt_id
        or interrupt_id != interrupt_id.strip()
        or len(interrupt_id) > 255
    ):
        return None
    value = first.get("value")
    tool_calls = value.get("tool_calls") if isinstance(value, dict) else None
    if not isinstance(tool_calls, list) or not tool_calls:
        return None
    actions: list[PublicInterruptAction] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            return None
        tool_name = call.get("name")
        args = call.get("args")
        if (
            not isinstance(tool_name, str)
            or not tool_name
            or tool_name != tool_name.strip()
            or len(tool_name) > 255
            or not isinstance(args, dict)
        ):
            return None
        memory = None
        if tool_name == "remember":
            normalized = normalize_remember_input(
                args.get("content"),
                args.get("kind", "semantic"),
            )
            if normalized is None or is_sensitive_memory(normalized.content):
                return None
            memory = PublicMemoryProposal(
                type=normalized.kind,
                content=normalized.content,
            )
        actions.append(
            PublicInterruptAction(
                tool_name=tool_name,
                purpose=(
                    "保存一条长期记忆" if tool_name == "remember" else f"运行工具 {tool_name}"
                ),
                memory=memory,
            )
        )
    if not actions:
        return None
    return PublicInterrupt(
        interrupt_id=interrupt_id,
        actions=actions,
    )


def public_interrupt(payload: dict[str, Any] | None) -> PublicInterrupt | None:
    """Return a safe approval projection for any checkpoint payload, without raising."""
    try:
        return _public_interrupt(payload)
    except Exception:
        return None


def public_interrupt_from_projection(value: Any) -> PublicInterrupt | None:
    """Revalidate an already projected interrupt through the same canonical rules."""
    try:
        if not isinstance(value, dict):
            return None
        interrupt_id = value.get("interrupt_id")
        actions = value.get("actions")
        if not isinstance(actions, list) or not actions:
            return None
        tool_calls: list[dict[str, Any]] = []
        normalized_sources: list[tuple[str, str] | None] = []
        for action in actions:
            if not isinstance(action, dict):
                return None
            tool_name = action.get("tool_name")
            if not isinstance(tool_name, str):
                return None
            memory = action.get("memory")
            if tool_name == "remember":
                if not isinstance(memory, dict):
                    return None
                memory_type = memory.get("type")
                content = memory.get("content")
                if not isinstance(memory_type, str) or not isinstance(content, str):
                    return None
                args = {"kind": memory_type, "content": content}
                normalized_sources.append((memory_type, content))
            else:
                if memory is not None:
                    return None
                args = {}
                normalized_sources.append(None)
            tool_calls.append({"name": tool_name, "args": args})
        canonical = public_interrupt(
            {
                "interrupts": [
                    {
                        "id": interrupt_id,
                        "value": {"tool_calls": tool_calls},
                    }
                ]
            }
        )
        if canonical is None or len(canonical.actions) != len(normalized_sources):
            return None
        for source, action in zip(normalized_sources, canonical.actions, strict=True):
            if source is None:
                continue
            if action.memory is None or source != (action.memory.type, action.memory.content):
                return None
        return canonical
    except Exception:
        return None


__all__ = [
    "interrupt_identity",
    "load_resume_value",
    "mark_resume_consumed",
    "pending_interrupt_descriptors",
    "pending_interrupt_ids",
    "public_interrupt",
    "public_interrupt_from_projection",
    "stable_json_hash",
]
