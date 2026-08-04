from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlmodel import Session, select

from contentai.agent.runtime.checkpoint import execution_checkpoint_config
from contentai.agent.runtime.turn_context import DurableTurnContext, load_turn_context_snapshot
from contentai.core.security import AuthContext
from contentai.db.session import get_engine
from contentai.models.agent import AgentVersion
from contentai.models.base import utcnow
from contentai.models.chat import (
    AgentExecution,
    AgentExecutionAttempt,
    AgentInvocation,
    ChatMessage,
    ChatSession,
    ExecutionOutbox,
    ExecutionResumeRequest,
)
from contentai.models.enums import ExecutionAttemptStatus, MessageRole, RunStatus
from contentai.models.user import AppUser
from contentai.services.execution_resume import load_resume_value, pending_interrupt_descriptors
from contentai.services.execution_settlement import settle_execution_cancellation

logger = logging.getLogger(__name__)
TERMINAL_STATUSES = {RunStatus.completed, RunStatus.failed, RunStatus.cancelled}


@dataclass(frozen=True)
class ClaimedExecution:
    auth: AuthContext
    turn_context: DurableTurnContext
    worker_id: str
    resume_value: Any = None
    resume_request_id: str | None = None
    continue_from_checkpoint: bool = False


def claim_execution(
    service: Any,
    execution_id: str,
    worker_id: str,
    *,
    model_config_id: str | None = None,
    create_attempt: bool = True,
    use_lease: bool = True,
) -> ClaimedExecution | None:
    with Session(get_engine(service.settings)) as session:
        execution = session.exec(
            select(AgentExecution).where(AgentExecution.id == execution_id).with_for_update()
        ).first()
        if execution is None or execution.status in TERMINAL_STATUSES:
            return None
        if model_config_id is not None and model_config_id != execution.model_config_id:
            return None
        now = utcnow()
        if execution.lease_expires_at is not None and execution.lease_expires_at > now:
            return None
        if execution.cancel_requested_at is not None:
            settle_execution_cancellation(session, execution, now=now)
            session.commit()
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

        outbox = session.exec(
            select(ExecutionOutbox)
            .where(ExecutionOutbox.execution_id == execution.id)
            .where(ExecutionOutbox.kind == "execute")
            .with_for_update()
        ).one_or_none()
        message_id = _snapshot_message_id(outbox.payload) if outbox is not None else None
        user_message = (
            session.exec(
                select(ChatMessage)
                .where(ChatMessage.id == message_id)
                .where(ChatMessage.invocation_id == invocation.id)
                .where(ChatMessage.role == MessageRole.user)
                .with_for_update()
            ).one_or_none()
            if message_id is not None
            else None
        )
        if user_message is None or outbox is None:
            _fail_execution(session, execution, "TURN_CONTEXT_SNAPSHOT_INVALID", now)
            return None
        if outbox.model_config_id != execution.model_config_id:
            _fail_execution(session, execution, "MODEL_CONFIGURATION_MISMATCH", now)
            return None
        try:
            turn_context = load_turn_context_snapshot(
                outbox.payload,
                execution_id=execution.id,
                invocation_id=invocation.id,
                session_id=chat.id,
                user_id=invocation.user_id,
                agent_id=chat.agent_id,
                agent_version_id=execution.agent_version_id,
                message_id=user_message.id,
            )
        except Exception:  # noqa: BLE001
            _fail_execution(session, execution, "TURN_CONTEXT_SNAPSHOT_INVALID", now)
            return None
        if turn_context.role != user.role:
            _fail_execution(session, execution, "TURN_CONTEXT_SNAPSHOT_INVALID", now)
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
                role=turn_context.role,
                allowed_agent_ids=(chat.agent_id,),
                tool_permissions=turn_context.tool_permissions,
            ),
            turn_context=turn_context,
            worker_id=worker_id,
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


def _snapshot_message_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    lineage = payload.get("lineage")
    message_id = lineage.get("message_id") if isinstance(lineage, dict) else None
    return message_id if isinstance(message_id, str) and message_id.strip() else None


def _cancel_disabled_user_execution(
    session: Session,
    execution: AgentExecution,
    now: Any,
) -> None:
    settle_execution_cancellation(
        session,
        execution,
        now=now,
        error="USER_DISABLED",
    )
    session.commit()


__all__ = ["ClaimedExecution", "claim_execution"]
