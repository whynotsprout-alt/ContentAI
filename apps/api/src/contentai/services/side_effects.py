from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlmodel import Session, select

from contentai.agent.runtime.context import ToolRuntimeContext, tool_runtime_scope
from contentai.agent.tools.memory import remember
from contentai.core.config import get_settings
from contentai.db.session import get_engine
from contentai.memory import LongTermMemory, MemoryRepository
from contentai.models.base import utcnow
from contentai.models.chat import (
    AgentExecution,
    AgentInvocation,
    ChatSession,
    SideEffectReceipt,
    ToolExecution,
)
from contentai.models.enums import ToolExecutionStatus
from contentai.services.execution_resume import stable_json_hash

logger = logging.getLogger(__name__)

SIDE_EFFECT_EXECUTION_FAILED = "SIDE_EFFECT_EXECUTION_FAILED"
SIDE_EFFECT_IDEMPOTENCY_MISMATCH = "SIDE_EFFECT_IDEMPOTENCY_MISMATCH"
SIDE_EFFECT_OUTCOME_UNKNOWN = "SIDE_EFFECT_OUTCOME_UNKNOWN"
DATABASE_SIDE_EFFECT_TOOLS = frozenset({"remember"})

OperationRunner = Callable[[Session, dict[str, Any]], dict[str, Any]]


def dispatch_side_effect_job(job: dict[str, Any]) -> None:
    from contentai.services.celery_app import celery_app

    execution_id = str(job.get("execution_id") or "")
    tool_call_id = str(job.get("tool_call_id") or "")
    celery_app.send_task(
        "contentai.execute_side_effect",
        kwargs={"job": job},
        queue=get_settings().agent.celery_side_effect_queue,
        task_id=f"side-effect:{execution_id}:{tool_call_id}",
    )


def execute_side_effect_job(
    job: dict[str, Any],
    *,
    operation_runner: OperationRunner | None = None,
) -> dict[str, Any]:
    execution_id = str(job.get("execution_id") or "")
    tool_call_id = str(job.get("tool_call_id") or "")
    try:
        with Session(get_engine()) as session:
            audit = session.exec(
                select(ToolExecution)
                .where(
                    ToolExecution.execution_id == execution_id,
                    ToolExecution.tool_call_id == tool_call_id,
                )
                .with_for_update()
            ).one()
            mismatch = _identity_mismatch(audit, job)
            if mismatch:
                session.rollback()
                return {"status": "failed", "error": SIDE_EFFECT_IDEMPOTENCY_MISMATCH}

            receipt = session.exec(
                select(SideEffectReceipt).where(
                    SideEffectReceipt.execution_id == execution_id,
                    SideEffectReceipt.tool_call_id == tool_call_id,
                )
            ).first()
            if receipt is not None and receipt.status in {"completed", "failed"}:
                return _receipt_outcome(receipt)
            if audit.status != ToolExecutionStatus.running:
                return {
                    "status": "failed",
                    "error": _stable_audit_error(audit.error),
                }

            _validate_lineage(session, job)
            runner = operation_runner or _execute_registered_operation
            result = _safe_result(str(job.get("tool_name") or ""), runner(session, job))
            result_digest = stable_json_hash(result)
            now = utcnow()
            if receipt is None:
                receipt = SideEffectReceipt(
                    execution_id=execution_id,
                    tool_call_id=tool_call_id,
                    operation=str(job.get("tool_name") or ""),
                )
            receipt.idempotency_key = stable_json_hash(
                {"execution_id": execution_id, "tool_call_id": tool_call_id}
            )
            receipt.status = "completed"
            receipt.result_digest = result_digest
            receipt.detail = {
                "arguments_hash": str(job.get("arguments_hash") or ""),
                "tool_version": str(job.get("tool_version") or ""),
                "result": result,
            }
            receipt.updated_at = now
            audit.status = ToolExecutionStatus.completed
            audit.result_digest = result_digest
            audit.error = ""
            audit.finished_at = now
            audit.touch_updated_at(now)
            session.add(receipt)
            session.add(audit)
            session.commit()
            return {"status": "completed", "result": result, "result_digest": result_digest}
    except Exception:  # noqa: BLE001
        logger.warning(
            "Side-effect execution failed: execution=%s tool_call=%s tool=%s",
            execution_id,
            tool_call_id,
            str(job.get("tool_name") or "unknown"),
        )
        _persist_failed_receipt(job)
        return {"status": "failed", "error": SIDE_EFFECT_EXECUTION_FAILED}


def read_side_effect_receipt(execution_id: str, tool_call_id: str) -> dict[str, Any] | None:
    with Session(get_engine()) as session:
        receipt = session.exec(
            select(SideEffectReceipt).where(
                SideEffectReceipt.execution_id == execution_id,
                SideEffectReceipt.tool_call_id == tool_call_id,
            )
        ).first()
        return None if receipt is None else _receipt_outcome(receipt)


def reconcile_stale_side_effects(
    *,
    older_than_seconds: int = 120,
    limit: int = 100,
) -> int:
    deadline = utcnow() - timedelta(seconds=max(1, int(older_than_seconds)))
    batch_size = max(1, min(int(limit), 500))
    reconciled = 0
    with Session(get_engine()) as session:
        audits = list(
            session.exec(
                select(ToolExecution)
                .where(
                    ToolExecution.status == ToolExecutionStatus.running,
                    ToolExecution.tool_name.in_(DATABASE_SIDE_EFFECT_TOOLS),
                    ToolExecution.updated_at < deadline,
                )
                .order_by(ToolExecution.updated_at, ToolExecution.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            ).all()
        )
        now = utcnow()
        for audit in audits:
            receipt = session.exec(
                select(SideEffectReceipt).where(
                    SideEffectReceipt.execution_id == audit.execution_id,
                    SideEffectReceipt.tool_call_id == audit.tool_call_id,
                )
            ).first()
            if receipt is not None and receipt.status not in {"completed", "failed"}:
                continue
            audit.finished_at = now
            if receipt is None:
                audit.status = ToolExecutionStatus.failed
                audit.result_digest = ""
                audit.error = SIDE_EFFECT_OUTCOME_UNKNOWN
            elif receipt.status == "completed":
                audit.status = ToolExecutionStatus.completed
                audit.result_digest = receipt.result_digest
                audit.error = ""
            else:
                detail = receipt.detail if isinstance(receipt.detail, dict) else {}
                receipt_error = str(detail.get("error") or "")
                audit.status = ToolExecutionStatus.failed
                audit.result_digest = ""
                audit.error = (
                    receipt_error
                    if receipt_error
                    in {SIDE_EFFECT_EXECUTION_FAILED, SIDE_EFFECT_OUTCOME_UNKNOWN}
                    else SIDE_EFFECT_EXECUTION_FAILED
                )
            audit.touch_updated_at(now)
            session.add(audit)
            reconciled += 1
        session.commit()
    return reconciled


def _execute_registered_operation(session: Session, job: dict[str, Any]) -> dict[str, Any]:
    tool_name = str(job.get("tool_name") or "")
    if tool_name not in DATABASE_SIDE_EFFECT_TOOLS:
        raise ValueError("Unsupported database side effect")
    execution = session.get(AgentExecution, str(job["execution_id"]))
    if execution is None:
        raise ValueError("Execution not found")
    invocation = session.get(AgentInvocation, execution.invocation_id)
    chat = session.get(ChatSession, execution.session_id)
    if invocation is None or chat is None:
        raise ValueError("Execution lineage is incomplete")
    runtime = ToolRuntimeContext(
        execution_id=execution.id,
        conversation_id=chat.id,
        session_id=chat.langgraph_thread_id,
        agent_id=chat.agent_id,
        agent_version_id=execution.agent_version_id,
        user_id=invocation.user_id,
        permissions=["remember"],
        long_term_memory=LongTermMemory(MemoryRepository(session, auto_commit=False)),
    )
    arguments = job.get("arguments")
    if not isinstance(arguments, dict):
        raise ValueError("Invalid arguments")
    with tool_runtime_scope(runtime):
        result = remember.invoke(arguments)
    if not isinstance(result, dict) or result.get("error"):
        raise ValueError("Remember operation rejected")
    return result


def _validate_lineage(session: Session, job: dict[str, Any]) -> None:
    execution = session.get(AgentExecution, str(job.get("execution_id") or ""))
    if execution is None:
        raise ValueError("Execution not found")
    invocation = session.get(AgentInvocation, execution.invocation_id)
    chat = session.get(ChatSession, execution.session_id)
    if invocation is None or chat is None:
        raise ValueError("Execution lineage is incomplete")
    expected = {
        "user_id": invocation.user_id,
        "agent_id": chat.agent_id,
        "agent_version_id": execution.agent_version_id,
        "session_id": chat.id,
        "thread_id": chat.langgraph_thread_id,
    }
    if any(str(job.get(key) or "") != str(value) for key, value in expected.items()):
        raise ValueError("Execution lineage mismatch")


def _identity_mismatch(audit: ToolExecution, job: dict[str, Any]) -> bool:
    return any(
        (
            audit.tool_name != str(job.get("tool_name") or ""),
            audit.tool_version != str(job.get("tool_version") or ""),
            audit.arguments_hash != str(job.get("arguments_hash") or ""),
        )
    )


def _stable_audit_error(error: str) -> str:
    return (
        error
        if error in {SIDE_EFFECT_EXECUTION_FAILED, SIDE_EFFECT_OUTCOME_UNKNOWN}
        else SIDE_EFFECT_OUTCOME_UNKNOWN
    )


def _safe_result(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "remember":
        return {
            "key": str(result.get("key") or "")[:200],
            "kind": str(result.get("kind") or "")[:40],
            "tool": "remember",
        }
    rendered = str(result)
    return {"output": rendered[:2000]}


def _receipt_outcome(receipt: SideEffectReceipt) -> dict[str, Any]:
    detail = receipt.detail if isinstance(receipt.detail, dict) else {}
    if receipt.status == "completed":
        result = detail.get("result")
        return {
            "status": "completed",
            "result": result if isinstance(result, dict) else {},
            "result_digest": receipt.result_digest,
        }
    return {
        "status": "failed",
        "error": str(detail.get("error") or SIDE_EFFECT_EXECUTION_FAILED),
    }


def _persist_failed_receipt(job: dict[str, Any]) -> None:
    execution_id = str(job.get("execution_id") or "")
    tool_call_id = str(job.get("tool_call_id") or "")
    with Session(get_engine()) as session:
        audit = session.exec(
            select(ToolExecution)
            .where(
                ToolExecution.execution_id == execution_id,
                ToolExecution.tool_call_id == tool_call_id,
            )
            .with_for_update()
        ).first()
        if audit is None or _identity_mismatch(audit, job):
            return
        receipt = session.exec(
            select(SideEffectReceipt).where(
                SideEffectReceipt.execution_id == execution_id,
                SideEffectReceipt.tool_call_id == tool_call_id,
            )
        ).first()
        if receipt is not None and receipt.status in {"completed", "failed"}:
            return
        now = utcnow()
        if receipt is None:
            receipt = SideEffectReceipt(
                execution_id=execution_id,
                tool_call_id=tool_call_id,
                operation=str(job.get("tool_name") or ""),
            )
        receipt.idempotency_key = stable_json_hash(
            {"execution_id": execution_id, "tool_call_id": tool_call_id}
        )
        receipt.status = "failed"
        receipt.result_digest = ""
        receipt.detail = {
            "arguments_hash": str(job.get("arguments_hash") or ""),
            "tool_version": str(job.get("tool_version") or ""),
            "error": SIDE_EFFECT_EXECUTION_FAILED,
        }
        receipt.updated_at = now
        audit.status = ToolExecutionStatus.failed
        audit.result_digest = ""
        audit.error = SIDE_EFFECT_EXECUTION_FAILED
        audit.finished_at = now
        audit.touch_updated_at(now)
        session.add(receipt)
        session.add(audit)
        session.commit()


__all__ = [
    "dispatch_side_effect_job",
    "execute_side_effect_job",
    "read_side_effect_receipt",
    "reconcile_stale_side_effects",
]
