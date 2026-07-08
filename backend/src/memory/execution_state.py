from __future__ import annotations

from typing import Any

from models.chat import AgentExecution
from models.enums import RunStatus
from agent.runtime.events import now_utc

from sqlmodel import Session


BUSY_RUN_STATUSES = {RunStatus.running}
TERMINAL_RUN_STATUSES = {
    RunStatus.completed,
    RunStatus.failed,
    RunStatus.cancelled,
    RunStatus.interrupted,
}


class ExecutionCancelled(Exception):
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
        execution.touch_updated_at(now)
        if status == RunStatus.running:
            execution.started_at = execution.started_at or now
        if status in TERMINAL_RUN_STATUSES:
            execution.finished_at = execution.finished_at or now
        db_session.add(execution)
        db_session.commit()

    def mark_interrupted(
        self,
        db_session: Session,
        execution: Any,
    ) -> None:
        self.set_execution_state(db_session, execution, RunStatus.interrupted, "")

    def ensure_execution_not_cancelled(
        self,
        db_session: Session,
        execution: Any,
    ) -> None:
        with Session(db_session.get_bind()) as fresh:
            if isinstance(execution, str):
                fresh_execution = fresh.get(AgentExecution, execution)
            else:
                fresh_execution = fresh.get(execution.__class__, execution.id)
            if fresh_execution is not None and fresh_execution.cancel_requested_at is not None:
                raise ExecutionCancelled()

__all__ = [
    "BUSY_RUN_STATUSES",
    "TERMINAL_RUN_STATUSES",
    "ExecutionCancelled",
    "ExecutionStateManager",
]
