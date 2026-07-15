from __future__ import annotations

import hashlib
import json
from typing import Any

from agent.runtime.checkpoint import checkpoint_interrupts
from models.base import utcnow
from models.chat import ExecutionResumeRequest
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


def stored_resume_value(value: Any) -> dict[str, Any]:
    return {"payload": value}


def load_resume_value(request: ExecutionResumeRequest) -> Any:
    return request.value.get("payload") if isinstance(request.value, dict) else None


def pending_interrupt_ids(checkpointer: Any, *, thread_id: str) -> set[str]:
    return set(pending_interrupt_descriptors(checkpointer, thread_id=thread_id))


def pending_interrupt_descriptors(
    checkpointer: Any,
    *,
    thread_id: str,
) -> dict[str, str]:
    output: dict[str, str] = {}
    for interrupt in checkpoint_interrupts(checkpointer, thread_id=thread_id):
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


def mark_resume_consumed(session: Session, request_id: str | None) -> None:
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
    session.commit()


__all__ = [
    "interrupt_identity",
    "load_resume_value",
    "mark_resume_consumed",
    "pending_interrupt_descriptors",
    "pending_interrupt_ids",
    "stable_json_hash",
    "stored_resume_value",
]
