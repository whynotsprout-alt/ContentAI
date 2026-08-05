from __future__ import annotations

import logging
import socket
import threading
from datetime import timedelta
from functools import lru_cache
from typing import Any

from celery.exceptions import Reject
from sqlalchemy import func
from sqlmodel import Session, select

from contentai.core.config import get_settings
from contentai.db.session import get_engine
from contentai.models.chat import AgentExecution, AgentExecutionAttempt, ExecutionOutbox
from contentai.models.enums import ExecutionAttemptKind, ExecutionAttemptStatus, RunStatus
from contentai.services.agent_service import AgentService
from contentai.services.celery_app import celery_app
from contentai.services.execution_claim import (
    CheckpointProbeUnstableError,
    ExecutionStreamSettlementPendingError,
    claim_execution,
)
from contentai.services.execution_settlement import (
    apply_streaming_degradation_first_wins,
    current_database_time,
    owns_active_execution_attempt,
    settle_execution_cancellation,
    stale_live_resume_requests,
)
from contentai.services.service_heartbeat import upsert_service_heartbeat
from contentai.services.side_effects import execute_side_effect_job, reconcile_stale_side_effects

logger = logging.getLogger(__name__)
TERMINAL_STATUSES = {RunStatus.completed, RunStatus.failed, RunStatus.cancelled}
SIDE_EFFECT_TASK_MAX_RETRIES = 3
CHECKPOINT_PROBE_TASK_MAX_RETRIES = 3
CLAIM_DELIVERY_OUTBOX_MAX_BACKOFF_SECONDS = 300
RECOVERY_BATCH_LIMIT = 100


@lru_cache(maxsize=1)
def _agent_service() -> AgentService:
    service = AgentService()
    service.start()
    return service


class WorkerHeartbeat:
    def __init__(
        self,
        execution_id: str,
        worker_id: str,
        attempt_id: str,
        service: AgentService,
    ) -> None:
        self.execution_id = execution_id
        self.worker_id = worker_id
        self.attempt_id = attempt_id
        self.service = service
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run, daemon=True, name=f"heartbeat-{execution_id}"
        )

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2)

    def _run(self) -> None:
        interval = max(5, int(self.service.settings.agent.worker_heartbeat_seconds))
        lease = int(self.service.settings.agent.worker_lease_seconds)
        while not self.stop_event.wait(interval):
            try:
                with Session(get_engine(self.service.settings)) as session:
                    execution = session.exec(
                        select(AgentExecution)
                        .where(AgentExecution.id == self.execution_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    ).one_or_none()
                    if execution is None:
                        return
                    renew_time = current_database_time(session)
                    if not owns_active_execution_attempt(
                        execution,
                        expected_worker_id=self.worker_id,
                        expected_attempt_id=self.attempt_id,
                        now=renew_time,
                    ):
                        session.rollback()
                        return
                    execution.heartbeat_at = renew_time
                    execution.lease_expires_at = renew_time + timedelta(seconds=lease)
                    execution.touch_updated_at(renew_time)
                    session.add(execution)
                    session.commit()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to renew execution lease: execution=%s worker=%s",
                    self.execution_id,
                    self.worker_id,
                    exc_info=True,
                )


@celery_app.task(name="contentai.record_queue_heartbeat", acks_late=True)
def record_queue_heartbeat(queue_name: str) -> None:
    """Beat routes this probe to a queue; only its real consumer can write it."""
    settings = get_settings()
    with Session(get_engine(settings)) as session:
        upsert_service_heartbeat(
            session,
            service_name="worker",
            queue_name=queue_name,
        )
        session.commit()


@celery_app.task(
    name="contentai.execute_side_effect",
    bind=True,
    acks_late=True,
    max_retries=SIDE_EFFECT_TASK_MAX_RETRIES,
)
def execute_side_effect(self: Any, *, job: dict[str, Any]) -> dict[str, Any]:
    try:
        return execute_side_effect_job(job)
    except Exception as exc:  # noqa: BLE001
        retry_count = max(0, int(getattr(self.request, "retries", 0)))
        raise self.retry(
            exc=exc,
            kwargs={"job": job},
            countdown=min(30, 2 ** min(retry_count, 4)),
            max_retries=SIDE_EFFECT_TASK_MAX_RETRIES,
        ) from exc


@celery_app.task(name="contentai.reconcile_side_effects")
def reconcile_side_effects() -> int:
    return reconcile_stale_side_effects()


@celery_app.task(
    name="contentai.execute_agent",
    bind=True,
    acks_late=True,
    max_retries=CHECKPOINT_PROBE_TASK_MAX_RETRIES,
)
def execute_agent(
    self: Any,
    *,
    execution_id: str,
    model_config_id: str,
    request_id: str | None = None,
) -> None:
    service = _agent_service()
    task_id = str(getattr(self.request, "id", "") or "unknown")
    worker_id = f"{socket.gethostname()}:{task_id}"
    try:
        claimed = claim_execution(
            service,
            execution_id,
            worker_id,
            model_config_id=model_config_id,
        )
    except (CheckpointProbeUnstableError, ExecutionStreamSettlementPendingError) as exc:
        retry_count = max(0, int(getattr(self.request, "retries", 0)))
        try:
            raise self.retry(
                exc=exc,
                kwargs={
                    "execution_id": execution_id,
                    "model_config_id": model_config_id,
                    "request_id": request_id,
                },
                countdown=min(30, 2 ** min(retry_count, 4)),
                max_retries=CHECKPOINT_PROBE_TASK_MAX_RETRIES,
            ) from exc
        except Reject as retry_reject:
            raise Reject(retry_reject.reason, requeue=True) from retry_reject
        except (CheckpointProbeUnstableError, ExecutionStreamSettlementPendingError) as exhausted:
            try:
                if _reschedule_unclaimed_delivery(
                    service,
                    execution_id=execution_id,
                    error=exhausted,
                ):
                    return
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to durably reschedule unclaimed execution %s.",
                    execution_id,
                )
            raise Reject(str(exhausted), requeue=True) from exhausted
    if claimed is None:
        return
    if claimed.attempt_id is None:
        raise RuntimeError("CLAIMED_EXECUTION_ATTEMPT_MISSING")

    heartbeat = WorkerHeartbeat(execution_id, worker_id, claimed.attempt_id, service)
    heartbeat.start()
    try:
        with Session(get_engine(service.settings)) as session:
            service.runner.run(
                session,
                execution_id=execution_id,
                worker_id=claimed.worker_id,
                attempt_id=claimed.attempt_id,
                auth=claimed.auth,
                tool_permissions=claimed.auth.tool_permissions,
                turn_context=claimed.turn_context,
                resume_value=claimed.resume_value,
                resume_request_id=claimed.resume_request_id,
                continue_from_checkpoint=claimed.continue_from_checkpoint,
                checkpoint_id=claimed.checkpoint_id,
                request_id=request_id,
            )
    finally:
        heartbeat.close()


def _reschedule_unclaimed_delivery(
    service: AgentService,
    *,
    execution_id: str,
    error: Exception,
) -> bool:
    """Return an exhausted claim retry to the durable outbox without claiming it."""
    with Session(get_engine(service.settings)) as session:
        execution = session.exec(
            select(AgentExecution)
            .where(AgentExecution.id == execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if execution is None or execution.status in TERMINAL_STATUSES:
            session.rollback()
            return True
        outbox = session.exec(
            select(ExecutionOutbox)
            .where(ExecutionOutbox.execution_id == execution_id)
            .where(ExecutionOutbox.kind == "execute")
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if outbox is None or outbox.status in {"cancelled", "failed"}:
            session.rollback()
            return False
        retry_time = current_database_time(session)
        outbox.processing_attempts += 1
        backoff_seconds = min(
            CLAIM_DELIVERY_OUTBOX_MAX_BACKOFF_SECONDS,
            2 ** min(outbox.processing_attempts, 8),
        )
        outbox.status = "pending"
        outbox.available_at = retry_time + timedelta(seconds=backoff_seconds)
        outbox.locked_by = None
        outbox.locked_until = None
        outbox.last_error = type(error).__name__
        outbox.updated_at = retry_time
        session.add(outbox)
        session.commit()
        logger.warning(
            "Execution claim retry exhausted; returned delivery to durable outbox: "
            "execution=%s cycle=%s backoff_seconds=%s reason=%s",
            execution_id,
            outbox.processing_attempts,
            backoff_seconds,
            type(error).__name__,
        )
        return True


@celery_app.task(name="contentai.recover_expired_executions")
def recover_expired_executions() -> int:
    settings = get_settings()
    recovered = 0
    with Session(get_engine(settings)) as session:
        now = current_database_time(session)
        claim_deadline = now - timedelta(
            seconds=settings.agent.worker_claim_timeout_seconds
        )
        # A task that has been published but never claimed has no lease, so it
        # is not covered by the lease-expiry branch below. Mark it failed after
        # a bounded wait instead of leaving the UI in a permanent pending state.
        unclaimed_rows = list(
            session.exec(
                select(AgentExecution)
                .join(
                    ExecutionOutbox,
                    (ExecutionOutbox.execution_id == AgentExecution.id)
                    & (ExecutionOutbox.kind == "execute"),
                )
                .where(AgentExecution.status == RunStatus.pending)
                .where(AgentExecution.claimed_at.is_(None))
                .where(ExecutionOutbox.status == "published")
                .where(ExecutionOutbox.published_at < claim_deadline)
                .order_by(ExecutionOutbox.published_at, AgentExecution.id)
                .limit(RECOVERY_BATCH_LIMIT)
                .with_for_update(skip_locked=True, of=AgentExecution)
            ).all()
        )
        unclaimed_now = current_database_time(session)
        for execution in unclaimed_rows:
            if execution.cancel_requested_at is not None:
                settle_execution_cancellation(session, execution, now=unclaimed_now)
                continue
            error = (
                "Agent worker did not claim the execution within "
                f"{settings.agent.worker_claim_timeout_seconds} seconds."
            )
            execution.status = RunStatus.failed
            execution.error = error
            execution.finished_at = unclaimed_now
            execution.touch_updated_at(unclaimed_now)
            apply_streaming_degradation_first_wins(
                execution,
                reason="TERMINAL_STREAM_NOT_PUBLISHED",
                now=unclaimed_now,
            )
            stale_live_resume_requests(session, execution.id, now=unclaimed_now)
            session.add(execution)
            logger.error("%s execution=%s", error, execution.id)

        expired_candidate_ids = list(
            session.exec(
                select(AgentExecution.id)
                .where(AgentExecution.status.in_({RunStatus.pending, RunStatus.running}))
                .where(AgentExecution.lease_expires_at.is_not(None))
                .where(AgentExecution.lease_expires_at < func.clock_timestamp())
                .order_by(AgentExecution.lease_expires_at, AgentExecution.id)
                .limit(RECOVERY_BATCH_LIMIT)
            ).all()
        )
        rows = list(
            session.exec(
                select(AgentExecution)
                .where(AgentExecution.id.in_(expired_candidate_ids))
                .order_by(AgentExecution.lease_expires_at, AgentExecution.id)
                .limit(RECOVERY_BATCH_LIMIT)
                .with_for_update(skip_locked=True, of=AgentExecution)
            ).all()
            if expired_candidate_ids
            else []
        )
        now = current_database_time(session)
        rows = [
            execution
            for execution in rows
            if execution.lease_expires_at is not None
            and execution.lease_expires_at <= now
            and execution.status in {RunStatus.pending, RunStatus.running}
        ]
        current_attempt_ids = sorted(
            {
                execution.current_attempt_id
                for execution in rows
                if execution.cancel_requested_at is None
                and execution.current_attempt_id is not None
            }
        )
        attempts_by_lineage = {
            (attempt.execution_id, attempt.id): attempt
            for attempt in (
                session.exec(
                    select(AgentExecutionAttempt).where(
                        AgentExecutionAttempt.id.in_(current_attempt_ids)
                    )
                ).all()
                if current_attempt_ids
                else []
            )
        }
        retry_execution_ids = sorted(
            execution.id
            for execution in rows
            if execution.cancel_requested_at is None
            and execution.attempt_count < settings.agent.max_execution_attempts
        )
        execute_outboxes_by_execution_id = {
            outbox.execution_id: outbox
            for outbox in (
                session.exec(
                    select(ExecutionOutbox).where(
                        ExecutionOutbox.execution_id.in_(retry_execution_ids),
                        ExecutionOutbox.kind == "execute",
                    )
                ).all()
                if retry_execution_ids
                else []
            )
        }
        for execution in rows:
            if execution.cancel_requested_at is not None:
                settle_execution_cancellation(session, execution, now=now)
                continue
            if execution.current_attempt_id:
                attempt = attempts_by_lineage.get(
                    (execution.id, execution.current_attempt_id)
                )
                if attempt is not None and attempt.finished_at is None:
                    attempt.status = ExecutionAttemptStatus.lease_lost
                    attempt.finished_at = now
                    session.add(attempt)
            if execution.attempt_count >= settings.agent.max_execution_attempts:
                execution.status = RunStatus.failed
                execution.error = "Execution retry limit exceeded after worker lease expiry."
                execution.finished_at = now
                apply_streaming_degradation_first_wins(
                    execution,
                    reason="TERMINAL_STREAM_NOT_PUBLISHED",
                    now=now,
                )
            else:
                execution.status = RunStatus.pending
                execution.worker_id = None
                execution.claimed_at = None
                execution.heartbeat_at = None
                execution.lease_expires_at = None
                execution.next_attempt_kind = ExecutionAttemptKind.retry
                outbox = execute_outboxes_by_execution_id.get(execution.id)
                if outbox is None:
                    execution.status = RunStatus.failed
                    execution.error = "TURN_CONTEXT_SNAPSHOT_INVALID"
                    execution.finished_at = now
                    apply_streaming_degradation_first_wins(
                        execution,
                        reason="TERMINAL_STREAM_NOT_PUBLISHED",
                        now=now,
                    )
                else:
                    outbox.status = "pending"
                    outbox.available_at = now
                    outbox.locked_by = None
                    outbox.locked_until = None
                    outbox.updated_at = now
                    session.add(outbox)
                    recovered += 1
            if execution.status == RunStatus.failed:
                stale_live_resume_requests(session, execution.id, now=now)
            execution.touch_updated_at(now)
            session.add(execution)

        stale_postprocess = list(
            session.exec(
                select(ExecutionOutbox)
                .join(
                    AgentExecution,
                    ExecutionOutbox.execution_id == AgentExecution.id,
                )
                .where(ExecutionOutbox.kind == "postprocess")
                .where(AgentExecution.postprocess_completed_at.is_(None))
                .where(
                    (ExecutionOutbox.status == "published")
                    & (ExecutionOutbox.published_at < claim_deadline)
                    | (
                        (ExecutionOutbox.status == "processing")
                        & (ExecutionOutbox.locked_until < now)
                    )
                )
                .order_by(ExecutionOutbox.created_at, ExecutionOutbox.id)
                .limit(RECOVERY_BATCH_LIMIT)
                .with_for_update(skip_locked=True, of=ExecutionOutbox)
            ).all()
        )
        for outbox in stale_postprocess:
            if outbox.processing_attempts >= settings.agent.postprocess_max_attempts:
                outbox.status = "failed"
                outbox.last_error = outbox.last_error or "Postprocess retry limit exceeded."
            else:
                outbox.status = "pending"
                outbox.available_at = now
                recovered += 1
            outbox.locked_by = None
            outbox.locked_until = None
            outbox.updated_at = now
            session.add(outbox)
        session.commit()
    return recovered


@celery_app.task(name="contentai.process_agent_post_execution", acks_late=True)
def process_agent_post_execution(
    *,
    execution_id: str,
    model_config_id: str,
    request_id: str | None = None,
) -> None:
    """Run low-priority title, summary, and memory work from durable execution data."""
    _agent_service().runner.post_service.process(
        execution_id=execution_id,
        model_config_id=model_config_id,
        request_id=request_id,
    )
