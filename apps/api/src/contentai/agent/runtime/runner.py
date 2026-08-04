from __future__ import annotations

import logging
from typing import Any

from contentai.agent.runtime.checkpoint import clear_execution_persistence
from contentai.agent.runtime.errors import classify_runtime_error
from contentai.agent.runtime.execution_services import (
    AgentExecutionEngine,
    AgentPostExecutionService,
    AgentRuntimeEventService,
)
from contentai.agent.runtime.turn_context import DurableTurnContext
from contentai.core.security import AGENT_WILDCARD, AuthContext
from contentai.memory.execution_state import (
    TERMINAL_RUN_STATUSES,
    ExecutionCancelled,
    ExecutionLeaseLost,
    ExecutionStateManager,
)
from contentai.models.base import utcnow
from contentai.models.chat import (
    AgentExecution,
    AgentExecutionAttempt,
    AgentInvocation,
    ChatMessage,
    ChatSession,
)
from contentai.models.enums import (
    ExecutionAttemptKind,
    ExecutionAttemptStatus,
    MessageRole,
    RunStatus,
)
from contentai.models.user import AppUser
from contentai.services.execution_resume import mark_resume_consumed
from contentai.services.execution_settlement import (
    finish_current_attempt,
    settle_execution_cancellation,
    settle_execution_failure,
)
from sqlmodel import Session, select

logger = logging.getLogger(__name__)


class AgentRunner:
    def __init__(self, container: Any) -> None:
        self.container = container
        self.state_manager = ExecutionStateManager()
        self.event_service = AgentRuntimeEventService(getattr(container, "settings", None))
        self.execution_engine = AgentExecutionEngine(self.container, self.state_manager)
        self.post_service = AgentPostExecutionService(
            container=self.container,
            settings=self.container.settings,
        )

    def close(self) -> None:
        self.post_service.close()

    def run(
        self,
        db_session: Session,
        *,
        execution_id: str,
        worker_id: str,
        event_writer: Any | None = None,
        tool_permissions: tuple[str, ...] = ("*",),
        turn_context: DurableTurnContext | None = None,
        resume_value: Any = None,
        resume_request_id: str | None = None,
        continue_from_checkpoint: bool = False,
        auth: AuthContext,
        request_id: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        tool_permissions = tuple(tool_permissions)
        if turn_context is None:
            raise RuntimeError("TURN_CONTEXT_SNAPSHOT_INVALID")
        loaded = self._load_execution_context(db_session, execution_id, auth, thread_id=thread_id)
        if loaded is None:
            return
        execution, invocation, chat, user_message = loaded

        if event_writer is None:
            event_writer = self.event_service.new_writer(
                db_session,
                execution,
                trace_id=execution.trace_id,
                settings=self.container.settings,
                thread_id=chat.langgraph_thread_id,
                conversation_id=chat.id,
                request_id=request_id,
            )
            owns_event_writer = True
        else:
            owns_event_writer = False

        try:
            user = db_session.exec(
                select(AppUser)
                .where(AppUser.id == auth.user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one_or_none()
            locked_execution = db_session.exec(
                select(AgentExecution)
                .where(AgentExecution.id == execution.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one()
            execution = locked_execution
            if user is None or user.status != "active":
                if execution.status in {RunStatus.completed, RunStatus.failed}:
                    db_session.rollback()
                else:
                    settle_execution_cancellation(
                        db_session,
                        execution,
                        now=utcnow(),
                        error="USER_DISABLED",
                    )
                    db_session.commit()
                return

            if execution.status in TERMINAL_RUN_STATUSES:
                attempt_status = {
                    RunStatus.completed: ExecutionAttemptStatus.completed,
                    RunStatus.failed: ExecutionAttemptStatus.failed,
                    RunStatus.cancelled: ExecutionAttemptStatus.cancelled,
                }[execution.status]
                finish_current_attempt(
                    db_session,
                    execution,
                    attempt_status,
                    now=utcnow(),
                )
                db_session.commit()
                return
            if execution.worker_id != worker_id:
                raise ExecutionLeaseLost()
            if execution.cancel_requested_at is not None:
                settle_execution_cancellation(
                    db_session,
                    execution,
                    now=utcnow(),
                )
                db_session.commit()
                return
            if execution.status != RunStatus.pending:
                db_session.rollback()
                return

            expected_worker_id = worker_id
            now = utcnow()
            execution.status = RunStatus.running
            execution.error = ""
            execution.interrupt_payload = {}
            execution.claimed_at = execution.claimed_at or now
            execution.heartbeat_at = now
            execution.started_at = execution.started_at or now
            execution.next_attempt_kind = ExecutionAttemptKind.retry
            execution.touch_updated_at(now)
            db_session.add(execution)
            db_session.commit()
            self._run_postcommit(
                lambda: self.event_service.emit_execution_started(event_writer, execution),
                execution_id=execution.id,
                operation="started event",
            )

            result = self.execution_engine.run_turn(
                db_session=db_session,
                execution=execution,
                invocation=invocation,
                chat=chat,
                user_message=user_message,
                tool_permissions=tool_permissions,
                turn_context=turn_context,
                resume_value=resume_value,
                continue_from_checkpoint=continue_from_checkpoint,
                event_writer=event_writer,
                event_service=self.event_service,
                request_id=request_id,
                expected_worker_id=expected_worker_id,
            )

            if result.interrupt_payload is not None:
                user = db_session.exec(
                    select(AppUser)
                    .where(AppUser.id == auth.user_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).one_or_none()
                locked_execution = db_session.exec(
                    select(AgentExecution)
                    .where(AgentExecution.id == execution.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                ).one()
                execution = locked_execution
                if user is None or user.status != "active":
                    if not settle_execution_cancellation(
                        db_session,
                        execution,
                        now=utcnow(),
                        error="USER_DISABLED",
                    ):
                        db_session.rollback()
                        return
                    db_session.commit()
                    self._run_postcommit(
                        lambda: self.event_service.emit_execution_cancelled(
                            event_writer,
                            execution,
                        ),
                        execution_id=execution.id,
                        operation="cancelled event",
                    )
                    return
                if execution.worker_id != expected_worker_id:
                    raise ExecutionLeaseLost()
                if (
                    execution.status == RunStatus.cancelled
                    or execution.cancel_requested_at is not None
                ):
                    if not settle_execution_cancellation(
                        db_session,
                        execution,
                        now=utcnow(),
                    ):
                        db_session.rollback()
                        return
                    db_session.commit()
                    self._run_postcommit(
                        lambda: self.event_service.emit_execution_cancelled(
                            event_writer,
                            execution,
                        ),
                        execution_id=execution.id,
                        operation="cancelled event",
                    )
                    return
                if execution.status != RunStatus.running:
                    raise ExecutionLeaseLost()

                now = utcnow()
                execution.status = RunStatus.waiting_input
                execution.error = ""
                execution.interrupt_payload = result.interrupt_payload
                execution.touch_updated_at(now)
                db_session.add(execution)
                mark_resume_consumed(db_session, resume_request_id)
                finish_current_attempt(
                    db_session,
                    execution,
                    ExecutionAttemptStatus.waiting_input,
                    now=now,
                )
                db_session.commit()
                self._run_postcommit(
                    lambda: self.event_service.emit_waiting_input(
                        event_writer,
                        execution,
                        result.interrupt_payload,
                    ),
                    execution_id=execution.id,
                    operation="waiting-input event",
                )
                return

            locked_execution = db_session.exec(
                select(AgentExecution)
                .where(AgentExecution.id == execution.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one()
            execution = locked_execution
            if execution.worker_id != expected_worker_id:
                raise ExecutionLeaseLost()
            if (
                execution.status == RunStatus.cancelled
                or execution.cancel_requested_at is not None
            ):
                raise ExecutionCancelled()
            if execution.status != RunStatus.running:
                raise ExecutionLeaseLost()
            now = utcnow()
            execution.status = RunStatus.completed
            execution.error = ""
            execution.interrupt_payload = {}
            execution.finished_at = execution.finished_at or now
            execution.touch_updated_at(now)
            db_session.add(execution)
            mark_resume_consumed(db_session, resume_request_id)
            self._finish_attempt(
                db_session,
                execution,
                ExecutionAttemptStatus.completed,
                commit=False,
            )
            self.post_service.enqueue(
                db_session=db_session,
                execution=execution,
                request_id=request_id,
            )
            db_session.commit()
            if result.assistant_message is not None:
                assistant_delta = {
                        "execution_id": execution.id,
                        "message_type": result.assistant_message.message_type,
                        "chunk": (
                            ""
                            if result.streamed_assistant_text
                            else result.assistant_message.content
                        ),
                        "done": True,
                    }
                self._run_postcommit(
                    lambda: event_writer.emit(
                        "assistant_message_delta",
                        assistant_delta,
                    ),
                    execution_id=execution.id,
                    operation="assistant delta event",
                )
                assistant_message = {
                        "execution_id": execution.id,
                        "message_id": result.assistant_message.id,
                        "message_type": result.assistant_message.message_type,
                        "content": result.assistant_message.content,
                    }
                self._run_postcommit(
                    lambda: event_writer.emit(
                        "assistant_message",
                        assistant_message,
                    ),
                    execution_id=execution.id,
                    operation="assistant message event",
                )
            self._run_postcommit(
                lambda: self.event_service.emit_execution_completed(event_writer, execution),
                execution_id=execution.id,
                operation="completed event",
            )
            try:
                clear_execution_persistence(
                    thread_id=chat.langgraph_thread_id,
                    execution_id=execution.id,
                    checkpointer=self.container.get_checkpointer(),
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Completed checkpoint cleanup failed for execution %s",
                    execution.id,
                    exc_info=True,
                )
            self._run_postcommit(
                lambda: self.post_service.schedule(
                    event_service=self.event_service,
                    event_writer=event_writer,
                    execution=execution,
                    request_id=request_id,
                ),
                execution_id=execution.id,
                operation="postprocess dispatch",
            )
        except ExecutionLeaseLost:
            db_session.rollback()
            logger.warning("Agent execution lease lost: %s", execution_id)
        except ExecutionCancelled:
            db_session.rollback()
            cancelled_execution = self._finalize_cancellation(
                db_session,
                execution_id=execution_id,
                expected_worker_id=worker_id,
            )
            if cancelled_execution is not None:
                execution = cancelled_execution
                self._run_postcommit(
                    lambda: self.event_service.emit_execution_cancelled(
                        event_writer,
                        execution,
                    ),
                    execution_id=execution.id,
                    operation="cancelled event",
                )
        except Exception as exc:  # noqa: BLE001
            db_session.rollback()
            logger.error(
                "Agent execution failed: execution=%s error_type=%s",
                execution.id,
                type(exc).__name__,
            )
            error_detail = classify_runtime_error(exc)
            settled_execution = self._finalize_failure(
                db_session,
                execution_id=execution_id,
                expected_worker_id=worker_id,
                user_id=auth.user_id,
                error=error_detail.message,
            )
            if settled_execution is not None:
                execution = settled_execution
                if execution.status == RunStatus.cancelled:
                    self._run_postcommit(
                        lambda: self.event_service.emit_execution_cancelled(
                            event_writer,
                            execution,
                        ),
                        execution_id=execution.id,
                        operation="cancelled event",
                    )
                else:
                    self._run_postcommit(
                        lambda: self.event_service.emit_execution_failed(
                            event_writer,
                            execution,
                            error_detail.message,
                            error_code=error_detail.code,
                            retryable=error_detail.retryable,
                        ),
                        execution_id=execution.id,
                        operation="failed event",
                    )
        finally:
            if owns_event_writer:
                close_writer = getattr(event_writer, "close", None)
                if callable(close_writer):
                    close_writer()

    def _finalize_cancellation(
        self,
        db_session: Session,
        *,
        execution_id: str,
        expected_worker_id: str,
    ) -> AgentExecution | None:
        execution = db_session.exec(
            select(AgentExecution)
            .where(AgentExecution.id == execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if (
            execution is None
            or execution.worker_id != expected_worker_id
            or execution.status in {RunStatus.completed, RunStatus.failed}
            or (
                execution.status != RunStatus.cancelled
                and execution.cancel_requested_at is None
            )
        ):
            db_session.rollback()
            return None

        settle_execution_cancellation(
            db_session,
            execution,
            now=utcnow(),
        )
        db_session.commit()
        return execution

    def _finalize_failure(
        self,
        db_session: Session,
        *,
        execution_id: str,
        expected_worker_id: str,
        user_id: str,
        error: str,
    ) -> AgentExecution | None:
        user = db_session.exec(
            select(AppUser)
            .where(AppUser.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        execution = db_session.exec(
            select(AgentExecution)
            .where(AgentExecution.id == execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if (
            execution is None
            or execution.worker_id != expected_worker_id
            or execution.status not in {RunStatus.pending, RunStatus.running}
        ):
            db_session.rollback()
            return None

        now = utcnow()
        if user is None or user.status != "active":
            settle_execution_cancellation(
                db_session,
                execution,
                now=now,
                error="USER_DISABLED",
            )
        elif execution.cancel_requested_at is not None:
            settle_execution_cancellation(
                db_session,
                execution,
                now=now,
            )
        else:
            settle_execution_failure(
                db_session,
                execution,
                now=now,
                error=error,
            )
        db_session.commit()
        return execution

    @staticmethod
    def _run_postcommit(
        action: Any,
        *,
        execution_id: str,
        operation: str,
    ) -> None:
        try:
            action()
        except Exception:  # noqa: BLE001
            logger.warning(
                "Execution postcommit %s failed: execution=%s",
                operation,
                execution_id,
                exc_info=True,
            )

    def _load_execution_context(
        self,
        db_session: Session,
        execution_id: str,
        auth: AuthContext,
        thread_id: str | None = None,
    ) -> tuple[AgentExecution, AgentInvocation, ChatSession, ChatMessage] | None:
        agent_filter = None
        if AGENT_WILDCARD not in auth.allowed_agent_ids:
            allowed_agents = [agent for agent in auth.allowed_agent_ids if agent]
            if allowed_agents:
                agent_filter = allowed_agents
            else:
                return None

        statement = (
            select(AgentExecution, AgentInvocation, ChatSession, ChatMessage)
            .join(AgentInvocation, AgentExecution.invocation_id == AgentInvocation.id)
            .join(ChatSession, AgentInvocation.session_id == ChatSession.id)
            .join(ChatMessage, ChatMessage.invocation_id == AgentInvocation.id)
            .where(AgentExecution.id == execution_id)
            .where(ChatSession.user_id == auth.user_id)
            .where(ChatMessage.role == MessageRole.user)
        )
        if agent_filter is not None:
            statement = statement.where(ChatSession.agent_id.in_(agent_filter))
        if thread_id is not None:
            statement = statement.where(ChatSession.langgraph_thread_id == thread_id)
        row = db_session.exec(statement).first()
        if row is None:
            return None
        return row

    @staticmethod
    def _finish_attempt(
        db_session: Session,
        execution: AgentExecution,
        status: ExecutionAttemptStatus,
        *,
        commit: bool = True,
    ) -> None:
        if not execution.current_attempt_id:
            return
        attempt = db_session.get(AgentExecutionAttempt, execution.current_attempt_id)
        if attempt is None or attempt.finished_at is not None:
            return
        attempt.status = status
        attempt.finished_at = utcnow()
        db_session.add(attempt)
        if commit:
            db_session.commit()
