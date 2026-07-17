from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from agent.runtime.checkpoint import execution_checkpoint_config
from core.security import AuthContext
from db.session import get_engine
from models.agent import AgentVersion
from models.base import utcnow
from models.chat import (
    AgentExecution,
    AgentExecutionAttempt,
    AgentInvocation,
    ChatSession,
    ExecutionOutbox,
    ExecutionResumeRequest,
)
from models.enums import ExecutionAttemptStatus, RunStatus
from models.user import AppUser
from services.execution_resume import load_resume_value, pending_interrupt_descriptors
from sqlmodel import Session, select

logger = logging.getLogger(__name__)
TERMINAL_STATUSES = {RunStatus.completed, RunStatus.failed, RunStatus.cancelled}


@dataclass(frozen=True)
class ClaimedExecution:
    auth: AuthContext
    resume_value: Any = None
    resume_request_id: str | None = None
    continue_from_checkpoint: bool = False


def claim_execution(
    service: Any,
    execution_id: str,
    worker_id: str,
    *,
    create_attempt: bool = True,
    use_lease: bool = True,
) -> ClaimedExecution | None:
    with Session(get_engine(service.settings)) as session:
        execution = session.exec(
            select(AgentExecution).where(AgentExecution.id == execution_id).with_for_update()
        ).first()
        if execution is None or execution.status in TERMINAL_STATUSES:
            return None
        now = utcnow()
        if execution.lease_expires_at is not None and execution.lease_expires_at > now:
            return None
        if execution.attempt_count >= int(service.settings.agent.max_execution_attempts):
            _fail_execution(session, execution, "Execution retry limit exceeded.", now)
            return None

        row = session.exec(
            select(AgentInvocation, ChatSession, AgentVersion)
            .join(ChatSession, AgentInvocation.session_id == ChatSession.id)
            .join(AgentExecution, AgentExecution.invocation_id == AgentInvocation.id)
            .join(AgentVersion, AgentExecution.agent_version_id == AgentVersion.id)
            .where(AgentExecution.id == execution_id)
        ).first()
        if row is None:
            _fail_execution(session, execution, "Execution context is incomplete.", now)
            return None
        invocation, chat, version = row
        if (
            invocation.agent_id != chat.agent_id
            or invocation.session_id != chat.id
            or invocation.user_id != chat.user_id
            or execution.session_id != chat.id
            or version.id != execution.agent_version_id
            or version.agent_id != chat.agent_id
            or execution.agent_version_id != chat.agent_version_id
        ):
            _fail_execution(session, execution, "EXECUTION_SCOPE_MISMATCH", now)
            return None

        user = session.get(AppUser, invocation.user_id)
        if user is None or user.status != "active":
            _cancel_disabled_user_execution(session, execution, now)
            return None

        resume_request = session.exec(
            select(ExecutionResumeRequest)
            .where(ExecutionResumeRequest.execution_id == execution.id)
            .where(ExecutionResumeRequest.status.in_(["pending", "claimed"]))
            .order_by(ExecutionResumeRequest.created_at.desc())
            .with_for_update()
        ).first()
        resume_value: Any = None
        continue_from_checkpoint = False
        if resume_request is not None:
            try:
                checkpointer = service.runtime.get_checkpointer()
                pending = pending_interrupt_descriptors(
                    checkpointer,
                    thread_id=chat.langgraph_thread_id,
                    execution_id=execution.id,
                )
            except Exception:
                logger.exception("Checkpoint validation failed for execution %s", execution.id)
                _fail_execution(session, execution, "CHECKPOINT_VALIDATION_FAILED", now)
                return None
            if resume_request.interrupt_id in pending:
                stored_hash = resume_request.tool_calls_hash
                if (
                    re.fullmatch(r"[0-9a-fA-F]{64}", stored_hash) is None
                    or pending[resume_request.interrupt_id] != stored_hash
                ):
                    resume_request.status = "stale"
                    resume_request.updated_at = now
                    session.add(resume_request)
                    _fail_execution(session, execution, "RUN_INTERRUPT_STALE", now)
                    return None
                resume_value = load_resume_value(resume_request)
            elif resume_request.status == "claimed":
                checkpoint = checkpointer.get_tuple(
                    execution_checkpoint_config(
                        thread_id=chat.langgraph_thread_id,
                        execution_id=execution.id,
                    )
                )
                if checkpoint is None:
                    resume_request.status = "stale"
                    resume_request.updated_at = now
                    session.add(resume_request)
                    _fail_execution(session, execution, "RUN_INTERRUPT_STALE", now)
                    return None
                continue_from_checkpoint = True
            else:
                resume_request.status = "stale"
                resume_request.updated_at = now
                session.add(resume_request)
                _fail_execution(session, execution, "RUN_INTERRUPT_STALE", now)
                return None
            resume_request.status = "claimed"
            resume_request.claimed_by = worker_id
            resume_request.claimed_at = now
            resume_request.updated_at = now
            session.add(resume_request)
        elif execution.attempt_count > 0:
            try:
                checkpoint = service.runtime.get_checkpointer().get_tuple(
                    execution_checkpoint_config(
                        thread_id=chat.langgraph_thread_id,
                        execution_id=execution.id,
                    )
                )
            except Exception:
                logger.exception("Checkpoint recovery lookup failed for execution %s", execution.id)
                _fail_execution(session, execution, "CHECKPOINT_RECOVERY_FAILED", now)
                return None
            continue_from_checkpoint = checkpoint is not None

        execution.worker_id = worker_id
        execution.status = RunStatus.pending
        execution.attempt_count += 1
        if create_attempt:
            attempt = AgentExecutionAttempt(
                execution_id=execution.id,
                ordinal=execution.attempt_count,
                kind=execution.next_attempt_kind,
                worker_id=worker_id,
                status=ExecutionAttemptStatus.running,
                started_at=now,
            )
            session.add(attempt)
            session.flush()
            execution.current_attempt_id = attempt.id
        execution.claimed_at = now
        execution.heartbeat_at = now
        execution.lease_expires_at = (
            now + timedelta(seconds=int(service.settings.agent.worker_lease_seconds))
            if use_lease
            else None
        )
        execution.touch_updated_at(now)
        session.add(execution)
        session.commit()
        return ClaimedExecution(
            auth=AuthContext(
                user_id=invocation.user_id,
                role=user.role,
                allowed_agent_ids=(chat.agent_id,),
                tool_permissions=("*",),
            ),
            resume_value=resume_value,
            resume_request_id=resume_request.id if resume_request is not None else None,
            continue_from_checkpoint=continue_from_checkpoint,
        )


def _fail_execution(
    session: Session,
    execution: AgentExecution,
    error: str,
    now: Any,
) -> None:
    execution.status = RunStatus.failed
    execution.error = error
    execution.finished_at = now
    execution.touch_updated_at(now)
    session.add(execution)
    session.commit()


def _cancel_disabled_user_execution(
    session: Session,
    execution: AgentExecution,
    now: Any,
) -> None:
    execution.status = RunStatus.cancelled
    execution.error = "USER_DISABLED"
    execution.finished_at = now
    execution.touch_updated_at(now)
    outbox = session.exec(
        select(ExecutionOutbox).where(
            ExecutionOutbox.execution_id == execution.id,
            ExecutionOutbox.kind == "execute",
        )
    ).first()
    if outbox is not None:
        outbox.status = "cancelled"
        outbox.updated_at = now
        session.add(outbox)
    session.add(execution)
    session.commit()


__all__ = ["ClaimedExecution", "claim_execution"]
