from __future__ import annotations

import logging
import socket
import threading
from datetime import timedelta
from functools import lru_cache
from typing import Any

from core.config import get_settings
from db.session import get_engine
from models.base import utcnow
from models.chat import AgentExecution, AgentExecutionAttempt, ExecutionOutbox
from models.enums import ExecutionAttemptKind, ExecutionAttemptStatus, RunStatus
from services.agent_service import AgentService
from services.celery_app import celery_app
from services.execution_claim import claim_execution
from services.execution_settlement import settle_execution_cancellation
from services.service_heartbeat import upsert_service_heartbeat
from services.side_effects import execute_side_effect_job, reconcile_stale_side_effects
from sqlmodel import Session, select

logger = logging.getLogger(__name__)
TERMINAL_STATUSES = {RunStatus.completed, RunStatus.failed, RunStatus.cancelled}
SIDE_EFFECT_TASK_MAX_RETRIES = 3


@lru_cache(maxsize=1)
def _agent_service() -> AgentService:
    service = AgentService()
    service.start()
    return service


class WorkerHeartbeat:
    def __init__(self, execution_id: str, worker_id: str, service: AgentService) -> None:
        self.execution_id = execution_id
        self.worker_id = worker_id
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
                    execution = session.get(AgentExecution, self.execution_id)
                    if (
                        execution is None
                        or execution.status in TERMINAL_STATUSES
                        or execution.worker_id != self.worker_id
                    ):
                        return
                    now = utcnow()
                    execution.heartbeat_at = now
                    execution.lease_expires_at = now + timedelta(seconds=lease)
                    execution.touch_updated_at(now)
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


@celery_app.task(name="contentai.execute_agent", bind=True, acks_late=True)
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
    claimed = claim_execution(
        service,
        execution_id,
        worker_id,
        model_config_id=model_config_id,
    )
    if claimed is None:
        return

    heartbeat = WorkerHeartbeat(execution_id, worker_id, service)
    heartbeat.start()
    try:
        with Session(get_engine(service.settings)) as session:
            service.runner.run(
                session,
                execution_id=execution_id,
                worker_id=claimed.worker_id,
                auth=claimed.auth,
                tool_permissions=claimed.auth.tool_permissions,
                turn_context=claimed.turn_context,
                resume_value=claimed.resume_value,
                resume_request_id=claimed.resume_request_id,
                continue_from_checkpoint=claimed.continue_from_checkpoint,
                request_id=request_id,
            )
    finally:
        heartbeat.close()


@celery_app.task(name="contentai.recover_expired_executions")
def recover_expired_executions() -> int:
    settings = get_settings()
    now = utcnow()
    recovered = 0
    claim_deadline = now - timedelta(seconds=settings.agent.worker_claim_timeout_seconds)
    with Session(get_engine(settings)) as session:
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
                .with_for_update(skip_locked=True)
            ).all()
        )
        for execution in unclaimed_rows:
            if execution.cancel_requested_at is not None:
                settle_execution_cancellation(session, execution, now=now)
                continue
            error = (
                "Agent worker did not claim the execution within "
                f"{settings.agent.worker_claim_timeout_seconds} seconds."
            )
            execution.status = RunStatus.failed
            execution.error = error
            execution.finished_at = now
            execution.touch_updated_at(now)
            session.add(execution)
            logger.error("%s execution=%s", error, execution.id)

        rows = list(
            session.exec(
                select(AgentExecution)
                .where(AgentExecution.status.in_({RunStatus.pending, RunStatus.running}))
                .where(AgentExecution.lease_expires_at.is_not(None))
                .where(AgentExecution.lease_expires_at < now)
                .with_for_update(skip_locked=True)
            ).all()
        )
        for execution in rows:
            if execution.cancel_requested_at is not None:
                settle_execution_cancellation(session, execution, now=now)
                continue
            if execution.current_attempt_id:
                attempt = session.get(AgentExecutionAttempt, execution.current_attempt_id)
                if attempt is not None and attempt.finished_at is None:
                    attempt.status = ExecutionAttemptStatus.lease_lost
                    attempt.finished_at = now
                    session.add(attempt)
            if execution.attempt_count >= settings.agent.max_execution_attempts:
                execution.status = RunStatus.failed
                execution.error = "Execution retry limit exceeded after worker lease expiry."
                execution.finished_at = now
            else:
                execution.status = RunStatus.pending
                execution.worker_id = None
                execution.claimed_at = None
                execution.heartbeat_at = None
                execution.lease_expires_at = None
                execution.next_attempt_kind = ExecutionAttemptKind.retry
                outbox = session.exec(
                    select(ExecutionOutbox).where(
                        ExecutionOutbox.execution_id == execution.id,
                        ExecutionOutbox.kind == "execute",
                    )
                ).first()
                if outbox is None:
                    execution.status = RunStatus.failed
                    execution.error = "TURN_CONTEXT_SNAPSHOT_INVALID"
                    execution.finished_at = now
                else:
                    outbox.status = "pending"
                    outbox.available_at = now
                    outbox.locked_by = None
                    outbox.locked_until = None
                    outbox.updated_at = now
                    session.add(outbox)
                    recovered += 1
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
                .with_for_update(skip_locked=True)
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
