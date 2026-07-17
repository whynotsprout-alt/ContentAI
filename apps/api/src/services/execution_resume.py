from __future__ import annotations

import hashlib
import json
from typing import Any

from agent.runtime.checkpoint import checkpoint_interrupts
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
    checkpointer: Any, *, thread_id: str, checkpoint_ns: str
) -> set[str]:
    return set(
        pending_interrupt_descriptors(
            checkpointer,
            thread_id=thread_id,
            checkpoint_ns=checkpoint_ns,
        )
    )


def pending_interrupt_descriptors(
    checkpointer: Any,
    *,
    thread_id: str,
    checkpoint_ns: str,
) -> dict[str, str]:
    output: dict[str, str] = {}
    for interrupt in checkpoint_interrupts(
        checkpointer,
        thread_id=thread_id,
        checkpoint_ns=checkpoint_ns,
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


def public_interrupt(payload: dict[str, Any] | None) -> PublicInterrupt | None:
    interrupts = payload.get("interrupts") if isinstance(payload, dict) else None
    first = interrupts[0] if isinstance(interrupts, list) and interrupts else None
    if not isinstance(first, dict):
        return None
    interrupt_id = str(first.get("id") or "").strip()
    value = first.get("value")
    tool_calls = value.get("tool_calls") if isinstance(value, dict) else None
    if not interrupt_id or not isinstance(tool_calls, list) or not tool_calls:
        return None
    actions: list[PublicInterruptAction] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        tool_name = str(call.get("name") or "unknown_tool").strip() or "unknown_tool"
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        memory = None
        if tool_name == "remember" and args.get("content"):
            memory = PublicMemoryProposal(
                type=str(args.get("kind") or "memory"),
                content=str(args["content"]),
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


__all__ = [
    "interrupt_identity",
    "load_resume_value",
    "mark_resume_consumed",
    "pending_interrupt_descriptors",
    "pending_interrupt_ids",
    "public_interrupt",
    "stable_json_hash",
]
