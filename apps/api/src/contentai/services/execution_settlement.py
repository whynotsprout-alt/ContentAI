from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, select

from contentai.models.chat import (
    AgentExecution,
    AgentExecutionAttempt,
    ExecutionOutbox,
    ExecutionResumeRequest,
)
from contentai.models.enums import ExecutionAttemptStatus, RunStatus


def current_database_time(session: Session) -> datetime:
    """Read the authorization clock after the caller has acquired its row lock."""
    return session.exec(select(func.clock_timestamp())).one()


def apply_streaming_degradation_first_wins(
    execution: AgentExecution,
    *,
    reason: str,
    now: datetime,
) -> bool:
    """Mutate an already row-locked execution without overwriting the first cause."""
    if execution.streaming_degraded:
        return False
    execution.streaming_degraded = True
    execution.streaming_degraded_at = now
    execution.streaming_degraded_reason = reason[:500]
    execution.touch_updated_at(now)
    return True


def mark_streaming_degraded_first_wins(
    session_bind: Any,
    execution_id: str,
    reason: str,
    *,
    expected_worker_id: str | None = None,
    expected_attempt_id: str | None = None,
) -> bool:
    """Persist one sticky stream failure under an exact row lock."""
    with Session(session_bind) as session:
        execution = session.exec(
            select(AgentExecution)
            .where(AgentExecution.id == execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if execution is None:
            session.rollback()
            return False
        if expected_worker_id is not None or expected_attempt_id is not None:
            if not matches_execution_attempt(
                execution,
                expected_worker_id=expected_worker_id,
                expected_attempt_id=expected_attempt_id,
            ):
                session.rollback()
                return False
        if execution.streaming_degraded:
            session.rollback()
            return False
        now = current_database_time(session)
        apply_streaming_degradation_first_wins(execution, reason=reason, now=now)
        session.add(execution)
        session.commit()
        return True


def matches_execution_attempt(
    execution: AgentExecution,
    *,
    expected_worker_id: str | None,
    expected_attempt_id: str | None,
) -> bool:
    if expected_worker_id is None and expected_attempt_id is None:
        return True
    return bool(
        isinstance(expected_worker_id, str)
        and expected_worker_id
        and expected_worker_id == expected_worker_id.strip()
        and isinstance(expected_attempt_id, str)
        and expected_attempt_id
        and expected_attempt_id == expected_attempt_id.strip()
        and execution.worker_id == expected_worker_id
        and execution.current_attempt_id == expected_attempt_id
    )


def owns_active_execution_attempt(
    execution: AgentExecution,
    *,
    expected_worker_id: str | None,
    expected_attempt_id: str | None,
    now: datetime,
) -> bool:
    return bool(
        matches_execution_attempt(
            execution,
            expected_worker_id=expected_worker_id,
            expected_attempt_id=expected_attempt_id,
        )
        and expected_worker_id is not None
        and expected_attempt_id is not None
        and execution.status in {RunStatus.pending, RunStatus.running}
        and execution.cancel_requested_at is None
        and execution.lease_expires_at is not None
        and execution.lease_expires_at > now
    )


def finish_current_attempt(
    session: Session,
    execution: AgentExecution,
    status: ExecutionAttemptStatus,
    *,
    now: datetime,
    expected_worker_id: str | None = None,
    expected_attempt_id: str | None = None,
    require_active_lease: bool = True,
) -> bool:
    owner_matches = matches_execution_attempt(
        execution,
        expected_worker_id=expected_worker_id,
        expected_attempt_id=expected_attempt_id,
    )
    if require_active_lease and (
        expected_worker_id is not None or expected_attempt_id is not None
    ):
        owner_matches = owns_active_execution_attempt(
            execution,
            expected_worker_id=expected_worker_id,
            expected_attempt_id=expected_attempt_id,
            now=now,
        )
    if not owner_matches or not execution.current_attempt_id:
        return False
    attempt = session.exec(
        select(AgentExecutionAttempt)
        .where(AgentExecutionAttempt.id == execution.current_attempt_id)
        .where(AgentExecutionAttempt.execution_id == execution.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if attempt is None or attempt.finished_at is not None:
        return False
    attempt.status = status
    attempt.finished_at = now
    session.add(attempt)
    return True


def settle_execution_cancellation(
    session: Session,
    execution: AgentExecution,
    *,
    now: datetime,
    error: str | None = None,
    expected_worker_id: str | None = None,
    expected_attempt_id: str | None = None,
    terminal_stream_will_publish: bool = False,
) -> bool:
    if not matches_execution_attempt(
        execution,
        expected_worker_id=expected_worker_id,
        expected_attempt_id=expected_attempt_id,
    ) or execution.status in {RunStatus.completed, RunStatus.failed}:
        return False
    execution.status = RunStatus.cancelled
    if error is not None:
        execution.error = error
    execution.interrupt_payload = {}
    execution.finished_at = execution.finished_at or now
    execution.touch_updated_at(now)
    _require_terminal_stream_outcome(
        execution,
        status=RunStatus.cancelled,
        now=now,
        terminal_stream_will_publish=terminal_stream_will_publish,
    )

    stale_live_resume_requests(session, execution.id, now=now)

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
        expected_worker_id=expected_worker_id,
        expected_attempt_id=expected_attempt_id,
        require_active_lease=False,
    )
    session.add(execution)
    return True


def stale_live_resume_requests(
    session: Session,
    execution_id: str,
    *,
    now: datetime,
) -> None:
    live_resume_requests = session.exec(
        select(ExecutionResumeRequest)
        .where(ExecutionResumeRequest.execution_id == execution_id)
        .where(ExecutionResumeRequest.status.in_(["pending", "claimed"]))
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    for resume_request in live_resume_requests:
        resume_request.status = "stale"
        resume_request.claimed_by = None
        resume_request.claimed_at = None
        resume_request.updated_at = now
        session.add(resume_request)


def settle_execution_failure(
    session: Session,
    execution: AgentExecution,
    *,
    now: datetime,
    error: str,
    expected_worker_id: str | None = None,
    expected_attempt_id: str | None = None,
    terminal_stream_will_publish: bool = False,
) -> bool:
    if not owns_active_execution_attempt(
        execution,
        expected_worker_id=expected_worker_id,
        expected_attempt_id=expected_attempt_id,
        now=now,
    ):
        return False
    if not finish_current_attempt(
        session,
        execution,
        ExecutionAttemptStatus.failed,
        now=now,
        expected_worker_id=expected_worker_id,
        expected_attempt_id=expected_attempt_id,
        require_active_lease=True,
    ):
        return False
    execution.status = RunStatus.failed
    execution.error = error
    execution.interrupt_payload = {}
    execution.finished_at = execution.finished_at or now
    execution.touch_updated_at(now)
    _require_terminal_stream_outcome(
        execution,
        status=RunStatus.failed,
        now=now,
        terminal_stream_will_publish=terminal_stream_will_publish,
    )
    stale_live_resume_requests(session, execution.id, now=now)
    session.add(execution)
    return True


def _require_terminal_stream_outcome(
    execution: AgentExecution,
    *,
    status: RunStatus,
    now: datetime,
    terminal_stream_will_publish: bool,
) -> None:
    if terminal_stream_will_publish:
        return
    terminal_is_durable = bool(
        execution.terminal_stream_sequence is not None
        and execution.terminal_stream_attempt_id
        and execution.terminal_stream_status == status.value
    )
    if not terminal_is_durable:
        apply_streaming_degradation_first_wins(
            execution,
            reason="TERMINAL_STREAM_NOT_PUBLISHED",
            now=now,
        )


__all__ = [
    "apply_streaming_degradation_first_wins",
    "current_database_time",
    "finish_current_attempt",
    "mark_streaming_degraded_first_wins",
    "matches_execution_attempt",
    "owns_active_execution_attempt",
    "settle_execution_cancellation",
    "settle_execution_failure",
    "stale_live_resume_requests",
]
