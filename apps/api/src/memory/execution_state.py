from __future__ import annotations

from typing import Any

from agent.runtime.events import now_utc
from models.chat import AgentExecution
from models.enums import RunStatus
from sqlmodel import Session

ACTIVE_RUN_STATUSES = {RunStatus.pending, RunStatus.running}
WAITING_RUN_STATUSES = {RunStatus.waiting_input}
BUSY_RUN_STATUSES = ACTIVE_RUN_STATUSES | WAITING_RUN_STATUSES
TERMINAL_RUN_STATUSES = {
    RunStatus.completed,
    RunStatus.failed,
    RunStatus.cancelled,
}


class ExecutionCancelled(Exception):
    pass


class ExecutionLeaseLost(Exception):
    pass


class ExecutionStateManager:
    def set_execution_state(
        self,
        db_session: Session,
        execution: Any,
        status: Any,
        error: str,
    ) -> None:
        now = now_utc()
        execution.status = status
        execution.error = error
        if status != RunStatus.waiting_input:
            execution.interrupt_payload = {}
        execution.touch_updated_at(now)
        if status == RunStatus.running:
            execution.started_at = execution.started_at or now
        if status in TERMINAL_RUN_STATUSES:
            execution.finished_at = execution.finished_at or now
        db_session.add(execution)
        db_session.commit()

    def mark_waiting_input(
        self,
        db_session: Session,
        execution: Any,
        interrupt_payload: dict[str, Any] | None = None,
    ) -> None:
        execution.interrupt_payload = interrupt_payload or {}
        self.set_execution_state(db_session, execution, RunStatus.waiting_input, "")

    def ensure_execution_not_cancelled(
        self,
        db_session: Session,
        execution: Any,
        *,
        expected_worker_id: str | None = None,
    ) -> None:
        with Session(db_session.get_bind()) as fresh:
            if isinstance(execution, str):
                fresh_execution = fresh.get(AgentExecution, execution)
            else:
                fresh_execution = fresh.get(execution.__class__, execution.id)
            if (
                fresh_execution is not None
                and expected_worker_id is not None
                and fresh_execution.worker_id != expected_worker_id
            ):
                raise ExecutionLeaseLost()
            if fresh_execution is not None and fresh_execution.cancel_requested_at is not None:
                raise ExecutionCancelled()


__all__ = [
    "ACTIVE_RUN_STATUSES",
    "BUSY_RUN_STATUSES",
    "WAITING_RUN_STATUSES",
    "TERMINAL_RUN_STATUSES",
    "ExecutionCancelled",
    "ExecutionLeaseLost",
    "ExecutionStateManager",
]
