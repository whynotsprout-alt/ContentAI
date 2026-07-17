from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from agent.runtime.checkpoint import clear_execution_persistence
from agent.runtime.errors import classify_runtime_error
from agent.runtime.execution_services import (
    AgentExecutionEngine,
    AgentPostExecutionService,
    AgentRuntimeEventService,
)
from core.security import AGENT_WILDCARD, AuthContext
from memory.execution_state import (
    TERMINAL_RUN_STATUSES,
    ExecutionCancelled,
    ExecutionLeaseLost,
    ExecutionStateManager,
)
from models.base import utcnow
from models.chat import (
    AgentExecution,
    AgentExecutionAttempt,
    AgentInvocation,
    ChatMessage,
    ChatSession,
)
from models.enums import ExecutionAttemptKind, ExecutionAttemptStatus, MessageRole, RunStatus
from services.execution_resume import mark_resume_consumed
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
        event_writer: Any | None = None,
        tool_permissions: tuple[str, ...] = ("*",),
        resume_value: Any = None,
        resume_request_id: str | None = None,
        continue_from_checkpoint: bool = False,
        auth: AuthContext,
        request_id: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        tool_permissions = tuple(tool_permissions)
        loaded = self._load_execution_context(db_session, execution_id, auth, thread_id=thread_id)
        if loaded is None:
            return
        execution, invocation, chat, user_message = loaded
        if execution.status in TERMINAL_RUN_STATUSES:
            return

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
            locked_execution = db_session.exec(
                select(AgentExecution).where(AgentExecution.id == execution.id).with_for_update()
            ).one()
            lease_seconds = int(getattr(self.container.settings.agent, "worker_lease_seconds", 120))
            if (
                locked_execution.status == RunStatus.running
                and locked_execution.heartbeat_at is not None
                and locked_execution.heartbeat_at > utcnow() - timedelta(seconds=lease_seconds)
            ):
                return
            execution = locked_execution
            expected_worker_id = execution.worker_id
            self.state_manager.ensure_execution_not_cancelled(
                db_session,
                execution,
                expected_worker_id=expected_worker_id,
            )
            execution.claimed_at = execution.claimed_at or utcnow()
            execution.heartbeat_at = utcnow()
            db_session.add(execution)
            db_session.commit()
            self.state_manager.set_execution_state(db_session, execution, RunStatus.running, "")
            self.event_service.emit_execution_started(event_writer, execution)
            execution.next_attempt_kind = ExecutionAttemptKind.retry
            db_session.add(execution)
            db_session.commit()

            result = self.execution_engine.run_turn(
                db_session=db_session,
                execution=execution,
                invocation=invocation,
                chat=chat,
                user_message=user_message,
                tool_permissions=tool_permissions,
                resume_value=resume_value,
                continue_from_checkpoint=continue_from_checkpoint,
                event_writer=event_writer,
                event_service=self.event_service,
                request_id=request_id,
                expected_worker_id=expected_worker_id,
            )

            if result.interrupt_payload is not None:
                mark_resume_consumed(db_session, resume_request_id)
                self.state_manager.mark_waiting_input(
                    db_session,
                    execution,
                    result.interrupt_payload,
                )
                self.event_service.emit_waiting_input(
                    event_writer,
                    execution,
                    result.interrupt_payload,
                )
                self._finish_attempt(db_session, execution, ExecutionAttemptStatus.waiting_input)
                return

            self.state_manager.ensure_execution_not_cancelled(
                db_session,
                execution,
                expected_worker_id=expected_worker_id,
            )
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
            db_session.commit()
            if result.assistant_message is not None:
                event_writer.emit(
                    "assistant_message_delta",
                    {
                        "execution_id": execution.id,
                        "message_type": result.assistant_message.message_type,
                        "chunk": (
                            ""
                            if result.streamed_assistant_text
                            else result.assistant_message.content
                        ),
                        "done": True,
                    },
                )
                event_writer.emit(
                    "assistant_message",
                    {
                        "execution_id": execution.id,
                        "message_id": result.assistant_message.id,
                        "message_type": result.assistant_message.message_type,
                        "content": result.assistant_message.content,
                    },
                )
            self.event_service.emit_execution_completed(event_writer, execution)
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
            self.state_manager.ensure_execution_not_cancelled(
                db_session,
                execution,
                expected_worker_id=expected_worker_id,
            )
            self.post_service.schedule(
                db_session=db_session,
                event_service=self.event_service,
                event_writer=event_writer,
                execution=execution,
                request_id=request_id,
            )
        except ExecutionLeaseLost:
            db_session.rollback()
            logger.warning("Agent execution lease lost: %s", execution.id)
        except ExecutionCancelled:
            self.state_manager.set_execution_state(db_session, execution, RunStatus.cancelled, "")
            self.event_service.emit_execution_cancelled(event_writer, execution)
            self._finish_attempt(db_session, execution, ExecutionAttemptStatus.cancelled)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Agent execution failed: %s", execution.id)
            error_detail = classify_runtime_error(exc)
            self.state_manager.set_execution_state(
                db_session, execution, RunStatus.failed, error_detail.message
            )
            self.event_service.emit_execution_failed(
                event_writer,
                execution,
                error_detail.message,
                error_code=error_detail.code,
                retryable=error_detail.retryable,
            )
            self._finish_attempt(db_session, execution, ExecutionAttemptStatus.failed)
        finally:
            if owns_event_writer:
                close_writer = getattr(event_writer, "close", None)
                if callable(close_writer):
                    close_writer()

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
