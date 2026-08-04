from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, select

from contentai.models.chat import (
    AgentExecution,
    AgentExecutionAttempt,
    ExecutionOutbox,
    ExecutionResumeRequest,
)
from contentai.models.enums import ExecutionAttemptStatus, RunStatus


def finish_current_attempt(
    session: Session,
    execution: AgentExecution,
    status: ExecutionAttemptStatus,
    *,
    now: datetime,
) -> None:
    if not execution.current_attempt_id:
        return
    attempt = session.exec(
        select(AgentExecutionAttempt)
        .where(AgentExecutionAttempt.id == execution.current_attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if attempt is None or attempt.finished_at is not None:
        return
    attempt.status = status
    attempt.finished_at = now
    session.add(attempt)


def settle_execution_cancellation(
    session: Session,
    execution: AgentExecution,
    *,
    now: datetime,
    error: str | None = None,
) -> bool:
    if execution.status in {RunStatus.completed, RunStatus.failed}:
        return False
    execution.status = RunStatus.cancelled
    if error is not None:
        execution.error = error
    execution.interrupt_payload = {}
    execution.finished_at = execution.finished_at or now
    execution.touch_updated_at(now)

    live_resume_requests = session.exec(
        select(ExecutionResumeRequest)
        .where(ExecutionResumeRequest.execution_id == execution.id)
        .where(ExecutionResumeRequest.status.in_(["pending", "claimed"]))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    for resume_request in live_resume_requests:
        resume_request.status = "stale"
        resume_request.updated_at = now
        session.add(resume_request)

    outbox = session.exec(
        select(ExecutionOutbox)
        .where(
            ExecutionOutbox.execution_id == execution.id,
            ExecutionOutbox.kind == "execute",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if outbox is not None:
        outbox.status = "cancelled"
        outbox.locked_by = None
        outbox.locked_until = None
        outbox.updated_at = now
        session.add(outbox)

    finish_current_attempt(
        session,
        execution,
        ExecutionAttemptStatus.cancelled,
        now=now,
    )
    session.add(execution)
    return True


def settle_execution_failure(
    session: Session,
    execution: AgentExecution,
    *,
    now: datetime,
    error: str,
) -> None:
    execution.status = RunStatus.failed
    execution.error = error
    execution.interrupt_payload = {}
    execution.finished_at = execution.finished_at or now
    execution.touch_updated_at(now)
    finish_current_attempt(
        session,
        execution,
        ExecutionAttemptStatus.failed,
        now=now,
    )
    session.add(execution)


__all__ = [
    "finish_current_attempt",
    "settle_execution_cancellation",
    "settle_execution_failure",
]
