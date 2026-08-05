from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from psycopg import InterfaceError as PsycopgInterfaceError
from psycopg import OperationalError as PsycopgOperationalError
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg_pool import PoolClosed, PoolTimeout
from sqlalchemy.exc import InterfaceError as SqlAlchemyInterfaceError
from sqlalchemy.exc import OperationalError as SqlAlchemyOperationalError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError
from sqlmodel import Session, select

from contentai.agent.runtime.checkpoint import execution_checkpoint_config
from contentai.agent.runtime.turn_context import DurableTurnContext, load_turn_context_snapshot
from contentai.core.security import AuthContext
from contentai.db.session import get_engine
from contentai.models.agent import AgentVersion
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
from contentai.services.execution_resume import (
    checkpoint_tuple_interrupt_descriptors,
    load_resume_value,
)
from contentai.services.execution_settlement import (
    apply_streaming_degradation_first_wins,
    current_database_time,
    settle_execution_cancellation,
)

logger = logging.getLogger(__name__)
TERMINAL_STATUSES = {RunStatus.completed, RunStatus.failed, RunStatus.cancelled}
_MAX_CHECKPOINT_PROBE_RETRIES = 3


class _RetryCheckpointProbe:
    pass


_RETRY_CHECKPOINT_PROBE = _RetryCheckpointProbe()


class CheckpointProbeUnstableError(RuntimeError):
    """Checkpoint state could not be fenced to one stable business snapshot."""


class ExecutionStreamSettlementPendingError(RuntimeError):
    """A resume delivery arrived before the previous attempt stream was closed."""


@dataclass(frozen=True)
class ClaimedExecution:
    auth: AuthContext
    turn_context: DurableTurnContext
    worker_id: str
    attempt_id: str | None = None
    resume_value: Any = None
    resume_request_id: str | None = None
    continue_from_checkpoint: bool = False
    checkpoint_id: str | None = None


@dataclass(frozen=True)
class _CheckpointProbe:
    execution_fence: tuple[Any, ...]
    thread_id: str
    resume_fence: tuple[Any, ...] | None


@dataclass(frozen=True)
class _CheckpointObservation:
    probe: _CheckpointProbe
    pending_interrupts: dict[str, str]
    checkpoint_exists: bool
    checkpoint_id: str | None = None
    error: str | None = None


def claim_execution(
    service: Any,
    execution_id: str,
    worker_id: str,
    *,
    model_config_id: str | None = None,
    create_attempt: bool = True,
    use_lease: bool = True,
) -> ClaimedExecution | None:
    for _attempt in range(_MAX_CHECKPOINT_PROBE_RETRIES):
        probe = _read_checkpoint_probe(
            service,
            execution_id,
            model_config_id=model_config_id,
        )
        observation = _observe_checkpoint(service, probe) if probe is not None else None
        result = _claim_execution_locked(
            service,
            execution_id,
            worker_id,
            model_config_id=model_config_id,
            create_attempt=create_attempt,
            use_lease=use_lease,
            observation=observation,
        )
        if result is _RETRY_CHECKPOINT_PROBE:
            continue
        return result
    logger.warning(
        "Execution %s changed repeatedly while checkpoint state was read; claim retry required.",
        execution_id,
    )
    raise CheckpointProbeUnstableError(
        f"Checkpoint probe did not stabilize for execution {execution_id}."
    )


def _claim_execution_locked(
    service: Any,
    execution_id: str,
    worker_id: str,
    *,
    model_config_id: str | None,
    create_attempt: bool,
    use_lease: bool,
    observation: _CheckpointObservation | None,
) -> ClaimedExecution | None | _RetryCheckpointProbe:
    with Session(get_engine(service.settings)) as session:
        execution = session.exec(
            select(AgentExecution).where(AgentExecution.id == execution_id).with_for_update()
        ).first()
        if execution is None or execution.status in TERMINAL_STATUSES:
            return None
        if model_config_id is not None and model_config_id != execution.model_config_id:
            return None
        authorization_time = current_database_time(session)
        if (
            execution.lease_expires_at is not None
            and execution.lease_expires_at > authorization_time
        ):
            return None
        if execution.cancel_requested_at is not None:
            settle_execution_cancellation(session, execution, now=authorization_time)
            session.commit()
            return None
        if execution.attempt_count >= int(service.settings.agent.max_execution_attempts):
            _fail_execution(
                session,
                execution,
                "Execution retry limit exceeded.",
                authorization_time,
            )
            return None

        row = session.exec(
            select(AgentInvocation, ChatSession, AgentVersion)
            .join(ChatSession, AgentInvocation.session_id == ChatSession.id)
            .join(AgentExecution, AgentExecution.invocation_id == AgentInvocation.id)
            .join(AgentVersion, AgentExecution.agent_version_id == AgentVersion.id)
            .where(AgentExecution.id == execution_id)
        ).first()
        if row is None:
            _fail_execution(
                session,
                execution,
                "Execution context is incomplete.",
                authorization_time,
            )
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
            _fail_execution(
                session,
                execution,
                "EXECUTION_SCOPE_MISMATCH",
                authorization_time,
            )
            return None

        user = session.get(AppUser, invocation.user_id)
        if user is None or user.status != "active":
            _cancel_disabled_user_execution(session, execution, authorization_time)
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
            _fail_execution(
                session,
                execution,
                "TURN_CONTEXT_SNAPSHOT_INVALID",
                authorization_time,
            )
            return None
        if outbox.model_config_id != execution.model_config_id:
            _fail_execution(
                session,
                execution,
                "MODEL_CONFIGURATION_MISMATCH",
                authorization_time,
            )
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
            _fail_execution(
                session,
                execution,
                "TURN_CONTEXT_SNAPSHOT_INVALID",
                authorization_time,
            )
            return None
        if turn_context.role != user.role:
            _fail_execution(
                session,
                execution,
                "TURN_CONTEXT_SNAPSHOT_INVALID",
                authorization_time,
            )
            return None

        resume_request = session.exec(
            select(ExecutionResumeRequest)
            .where(ExecutionResumeRequest.execution_id == execution.id)
            .where(ExecutionResumeRequest.status.in_(["pending", "claimed"]))
            .order_by(
                ExecutionResumeRequest.created_at.desc(),
                ExecutionResumeRequest.id.desc(),
            )
            .with_for_update()
        ).first()
        resume_value: Any = None
        continue_from_checkpoint = False
        checkpoint_id: str | None = None
        checkpoint_required = resume_request is not None or execution.attempt_count > 0
        if checkpoint_required:
            if observation is None or not _checkpoint_probe_matches(
                observation.probe,
                execution=execution,
                chat=chat,
                resume_request=resume_request,
            ):
                return _RETRY_CHECKPOINT_PROBE
            if observation.error is not None:
                logger.error(
                    "Checkpoint lookup failed for execution %s before its claim transaction.",
                    execution.id,
                )
                _fail_execution(
                    session,
                    execution,
                    observation.error,
                    authorization_time,
                )
                return None
            checkpoint_id = observation.checkpoint_id
        elif observation is not None:
            return _RETRY_CHECKPOINT_PROBE

        checkpoint_error = (
            "CHECKPOINT_VALIDATION_FAILED"
            if resume_request is not None
            else "CHECKPOINT_RECOVERY_FAILED"
        )
        if execution.latest_checkpoint_id is not None:
            if (
                observation is None
                or not observation.checkpoint_exists
                or checkpoint_id != execution.latest_checkpoint_id
            ):
                _fail_execution(
                    session,
                    execution,
                    checkpoint_error,
                    authorization_time,
                )
                return None
        elif observation is not None and observation.checkpoint_exists:
            if execution.checkpoint_revision != 0 or checkpoint_id is None:
                _fail_execution(
                    session,
                    execution,
                    checkpoint_error,
                    authorization_time,
                )
                return None
            execution.latest_checkpoint_id = checkpoint_id
            session.add(execution)

        if resume_request is not None:
            assert observation is not None
            if resume_request.interrupt_id in observation.pending_interrupts:
                stored_hash = resume_request.tool_calls_hash
                if (
                    not isinstance(stored_hash, str)
                    or re.fullmatch(r"[0-9a-fA-F]{64}", stored_hash) is None
                    or observation.pending_interrupts[resume_request.interrupt_id] != stored_hash
                ):
                    resume_request.status = "stale"
                    resume_request.updated_at = authorization_time
                    session.add(resume_request)
                    _fail_execution(
                        session,
                        execution,
                        "RUN_INTERRUPT_STALE",
                        authorization_time,
                    )
                    return None
                resume_value = load_resume_value(resume_request)
            elif resume_request.status == "claimed":
                if not observation.checkpoint_exists:
                    resume_request.status = "stale"
                    resume_request.updated_at = authorization_time
                    session.add(resume_request)
                    _fail_execution(
                        session,
                        execution,
                        "RUN_INTERRUPT_STALE",
                        authorization_time,
                    )
                    return None
                continue_from_checkpoint = True
            else:
                resume_request.status = "stale"
                resume_request.updated_at = authorization_time
                session.add(resume_request)
                _fail_execution(
                    session,
                    execution,
                    "RUN_INTERRUPT_STALE",
                    authorization_time,
                )
                return None
        elif execution.attempt_count > 0:
            assert observation is not None
            continue_from_checkpoint = observation.checkpoint_exists

        if resume_request is not None and execution.current_attempt_id is not None:
            prior_attempt = session.exec(
                select(AgentExecutionAttempt)
                .where(AgentExecutionAttempt.id == execution.current_attempt_id)
                .where(AgentExecutionAttempt.execution_id == execution.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one_or_none()
            if (
                prior_attempt is not None
                and prior_attempt.status == ExecutionAttemptStatus.waiting_input
                and not execution.streaming_degraded
            ):
                if execution.terminal_stream_sequence is None:
                    raise ExecutionStreamSettlementPendingError(
                        f"Waiting-input stream is not closed for execution {execution.id}."
                    )
                if (
                    execution.terminal_stream_attempt_id != prior_attempt.id
                    or execution.terminal_stream_status != "waiting_input"
                ):
                    _fail_execution(
                        session,
                        execution,
                        "STREAM_REPLAY_GAP",
                        authorization_time,
                    )
                    return None

        claim_time = current_database_time(session)
        if resume_request is not None:
            resume_request.status = "claimed"
            resume_request.claimed_by = worker_id
            resume_request.claimed_at = claim_time
            resume_request.updated_at = claim_time
            session.add(resume_request)

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
                started_at=claim_time,
            )
            session.add(attempt)
            session.flush()
            execution.current_attempt_id = attempt.id
        execution.claimed_at = claim_time
        execution.heartbeat_at = claim_time
        execution.lease_expires_at = (
            claim_time + timedelta(seconds=int(service.settings.agent.worker_lease_seconds))
            if use_lease
            else None
        )
        execution.touch_updated_at(claim_time)
        attempt_id = execution.current_attempt_id
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
            attempt_id=attempt_id,
            resume_value=resume_value,
            resume_request_id=resume_request.id if resume_request is not None else None,
            continue_from_checkpoint=continue_from_checkpoint,
            checkpoint_id=checkpoint_id,
        )


def _read_checkpoint_probe(
    service: Any,
    execution_id: str,
    *,
    model_config_id: str | None,
) -> _CheckpointProbe | None:
    """Read only the business state needed by checkpoint I/O, without row locks."""
    with Session(get_engine(service.settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        if execution is None or execution.status in TERMINAL_STATUSES:
            return None
        if model_config_id is not None and model_config_id != execution.model_config_id:
            return None
        probe_time = current_database_time(session)
        if (
            execution.lease_expires_at is not None
            and execution.lease_expires_at > probe_time
        ):
            return None
        if execution.cancel_requested_at is not None:
            return None
        if execution.attempt_count >= int(service.settings.agent.max_execution_attempts):
            return None
        chat = session.get(ChatSession, execution.session_id)
        if chat is None:
            return None
        resume_request = session.exec(
            select(ExecutionResumeRequest)
            .where(ExecutionResumeRequest.execution_id == execution.id)
            .where(ExecutionResumeRequest.status.in_(["pending", "claimed"]))
            .order_by(
                ExecutionResumeRequest.created_at.desc(),
                ExecutionResumeRequest.id.desc(),
            )
        ).first()
        if resume_request is None and execution.attempt_count <= 0:
            return None
        return _CheckpointProbe(
            execution_fence=_execution_checkpoint_fence(execution),
            thread_id=chat.langgraph_thread_id,
            resume_fence=_resume_checkpoint_fence(resume_request),
        )


def _observe_checkpoint(
    service: Any,
    probe: _CheckpointProbe,
) -> _CheckpointObservation:
    """Access the independent checkpoint pool before any business row is locked."""
    error = (
        "CHECKPOINT_VALIDATION_FAILED"
        if probe.resume_fence is not None
        else "CHECKPOINT_RECOVERY_FAILED"
    )
    try:
        checkpointer = service.runtime.get_checkpointer()
        checkpoint = checkpointer.get_tuple(
            execution_checkpoint_config(
                thread_id=probe.thread_id,
                execution_id=_probe_execution_id(probe),
            )
        )
        checkpoint_id = _checkpoint_id(checkpoint)
        if checkpoint is not None and checkpoint_id is None:
            return _CheckpointObservation(
                probe=probe,
                pending_interrupts={},
                checkpoint_exists=True,
                error=error,
            )
        return _CheckpointObservation(
            probe=probe,
            pending_interrupts=checkpoint_tuple_interrupt_descriptors(checkpoint),
            checkpoint_exists=checkpoint is not None,
            checkpoint_id=checkpoint_id,
        )
    except Exception as exc:
        if _is_transient_checkpoint_probe_error(exc):
            logger.warning(
                "Transient checkpoint lookup failure for execution %s; delivery retry required.",
                _probe_execution_id(probe),
                exc_info=True,
            )
            raise CheckpointProbeUnstableError(
                f"Checkpoint probe was temporarily unavailable for execution "
                f"{_probe_execution_id(probe)}."
            ) from exc
        logger.exception("Checkpoint lookup failed for execution %s", _probe_execution_id(probe))
        return _CheckpointObservation(
            probe=probe,
            pending_interrupts={},
            checkpoint_exists=False,
            error=error,
        )


def _is_transient_checkpoint_probe_error(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            ConnectionError,
            TimeoutError,
            OSError,
            PoolClosed,
            PoolTimeout,
            PsycopgInterfaceError,
            PsycopgOperationalError,
            DeadlockDetected,
            SerializationFailure,
            SqlAlchemyInterfaceError,
            SqlAlchemyOperationalError,
            SqlAlchemyTimeoutError,
        ),
    )


def _checkpoint_probe_matches(
    probe: _CheckpointProbe,
    *,
    execution: AgentExecution,
    chat: ChatSession,
    resume_request: ExecutionResumeRequest | None,
) -> bool:
    return (
        probe.execution_fence == _execution_checkpoint_fence(execution)
        and probe.thread_id == chat.langgraph_thread_id
        and probe.resume_fence == _resume_checkpoint_fence(resume_request)
    )


def _execution_checkpoint_fence(execution: AgentExecution) -> tuple[Any, ...]:
    return (
        execution.id,
        execution.invocation_id,
        execution.session_id,
        execution.agent_version_id,
        execution.model_config_id,
        execution.latest_checkpoint_id,
        execution.checkpoint_revision,
        execution.status,
        execution.worker_id,
        execution.claimed_at,
        execution.heartbeat_at,
        execution.lease_expires_at,
        execution.cancel_requested_at,
        execution.attempt_count,
        execution.next_attempt_kind,
        execution.current_attempt_id,
        execution.updated_at,
    )


def _resume_checkpoint_fence(
    resume_request: ExecutionResumeRequest | None,
) -> tuple[Any, ...] | None:
    if resume_request is None:
        return None
    return (
        resume_request.id,
        resume_request.execution_id,
        resume_request.interrupt_id,
        resume_request.decision,
        resume_request.message_id,
        resume_request.tool_calls_hash,
        resume_request.value,
        resume_request.status,
        resume_request.claimed_by,
        resume_request.claimed_at,
        resume_request.consumed_at,
        resume_request.created_at,
        resume_request.updated_at,
    )


def _probe_execution_id(probe: _CheckpointProbe) -> str:
    return str(probe.execution_fence[0])


def _checkpoint_id(checkpoint: Any) -> str | None:
    if checkpoint is None:
        return None
    config = getattr(checkpoint, "config", None)
    configurable = config.get("configurable") if isinstance(config, dict) else None
    checkpoint_id = (
        configurable.get("checkpoint_id") if isinstance(configurable, dict) else None
    )
    if (
        not isinstance(checkpoint_id, str)
        or not checkpoint_id
        or checkpoint_id != checkpoint_id.strip()
    ):
        return None
    return checkpoint_id


def _fail_execution(
    session: Session,
    execution: AgentExecution,
    error: str,
    now: Any,
) -> None:
    if execution.current_attempt_id is not None:
        current_attempt = session.exec(
            select(AgentExecutionAttempt)
            .where(AgentExecutionAttempt.id == execution.current_attempt_id)
            .where(AgentExecutionAttempt.execution_id == execution.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if (
            current_attempt is not None
            and current_attempt.status == ExecutionAttemptStatus.running
            and current_attempt.finished_at is None
        ):
            current_attempt.status = ExecutionAttemptStatus.lease_lost
            current_attempt.finished_at = now
            session.add(current_attempt)

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
        .where(ExecutionOutbox.execution_id == execution.id)
        .where(ExecutionOutbox.kind == "execute")
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if outbox is not None:
        outbox.status = "failed"
        outbox.locked_by = None
        outbox.locked_until = None
        outbox.last_error = error[:2000]
        outbox.updated_at = now
        session.add(outbox)

    execution.status = RunStatus.failed
    execution.error = error
    execution.finished_at = now
    execution.touch_updated_at(now)
    apply_streaming_degradation_first_wins(
        execution,
        reason="TERMINAL_STREAM_NOT_PUBLISHED",
        now=now,
    )
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


__all__ = [
    "CheckpointProbeUnstableError",
    "ClaimedExecution",
    "ExecutionStreamSettlementPendingError",
    "claim_execution",
]
