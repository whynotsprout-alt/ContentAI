from __future__ import annotations

import logging
from typing import Any

from contentai.agent.runtime.checkpoint import (
    CheckpointOwnershipLostError,
    CheckpointSupersededError,
    clear_execution_persistence,
)
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
from contentai.models.chat import (
    AgentExecution,
    AgentInvocation,
    ChatMessage,
    ChatSession,
    ExecutionOutbox,
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
    apply_streaming_degradation_first_wins,
    current_database_time,
    finish_current_attempt,
    matches_execution_attempt,
    owns_active_execution_attempt,
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
        attempt_id: str | None = None,
        event_writer: Any | None = None,
        tool_permissions: tuple[str, ...] = ("*",),
        turn_context: DurableTurnContext | None = None,
        resume_value: Any = None,
        resume_request_id: str | None = None,
        continue_from_checkpoint: bool = False,
        checkpoint_id: str | None = None,
        auth: AuthContext,
        request_id: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        tool_permissions = tuple(tool_permissions)
        if turn_context is None:
            raise RuntimeError("TURN_CONTEXT_SNAPSHOT_INVALID")
        if (
            not isinstance(worker_id, str)
            or not worker_id
            or worker_id != worker_id.strip()
            or not isinstance(attempt_id, str)
            or not attempt_id
            or attempt_id != attempt_id.strip()
        ):
            db_session.rollback()
            logger.warning("Agent execution attempt fence is missing: %s", execution_id)
            return
        loaded = self._load_execution_context(db_session, execution_id, auth, thread_id=thread_id)
        if loaded is None:
            return
        execution, invocation, chat, user_message = loaded
        expected_worker_id = worker_id
        expected_attempt_id = attempt_id

        if event_writer is None:
            event_writer = self.event_service.new_writer(
                db_session,
                execution,
                trace_id=execution.trace_id,
                settings=self.container.settings,
                thread_id=chat.langgraph_thread_id,
                conversation_id=chat.id,
                request_id=request_id,
                expected_worker_id=expected_worker_id,
                expected_attempt_id=expected_attempt_id,
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
            authorization_time = current_database_time(db_session)
            if not matches_execution_attempt(
                execution,
                expected_worker_id=expected_worker_id,
                expected_attempt_id=expected_attempt_id,
            ):
                raise ExecutionLeaseLost()
            if user is None or user.status != "active":
                if execution.status in {RunStatus.completed, RunStatus.failed}:
                    db_session.rollback()
                else:
                    self._prepare_event_settlement(event_writer)
                    settle_execution_cancellation(
                        db_session,
                        execution,
                        now=authorization_time,
                        error="USER_DISABLED",
                        expected_worker_id=expected_worker_id,
                        expected_attempt_id=expected_attempt_id,
                        terminal_stream_will_publish=True,
                    )
                    self._commit_terminal_settlement(
                        db_session,
                        event_writer,
                        self.event_service.execution_cancelled_events(execution),
                        execution=execution,
                    )
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
                    now=authorization_time,
                    expected_worker_id=expected_worker_id,
                    expected_attempt_id=expected_attempt_id,
                    require_active_lease=False,
                )
                if (
                    execution.terminal_stream_sequence is None
                    and not execution.streaming_degraded
                ):
                    apply_streaming_degradation_first_wins(
                        execution,
                        reason="TERMINAL_STREAM_NOT_PUBLISHED",
                        now=authorization_time,
                    )
                    db_session.add(execution)
                db_session.commit()
                return
            if execution.cancel_requested_at is not None:
                self._prepare_event_settlement(event_writer)
                settle_execution_cancellation(
                    db_session,
                    execution,
                    now=authorization_time,
                    expected_worker_id=expected_worker_id,
                    expected_attempt_id=expected_attempt_id,
                    terminal_stream_will_publish=True,
                )
                self._commit_terminal_settlement(
                    db_session,
                    event_writer,
                    self.event_service.execution_cancelled_events(execution),
                    execution=execution,
                )
                return
            if not owns_active_execution_attempt(
                execution,
                expected_worker_id=expected_worker_id,
                expected_attempt_id=expected_attempt_id,
                now=authorization_time,
            ):
                raise ExecutionLeaseLost()
            if execution.status != RunStatus.pending:
                db_session.rollback()
                return

            now = authorization_time
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
                checkpoint_id=checkpoint_id,
                event_writer=event_writer,
                event_service=self.event_service,
                request_id=request_id,
                expected_worker_id=expected_worker_id,
                expected_attempt_id=expected_attempt_id,
            )

            if result.interrupt_payload is not None:
                self._prepare_event_settlement(event_writer)
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
                authorization_time = current_database_time(db_session)
                if user is None or user.status != "active":
                    if not settle_execution_cancellation(
                        db_session,
                        execution,
                        now=authorization_time,
                        error="USER_DISABLED",
                        expected_worker_id=expected_worker_id,
                        expected_attempt_id=expected_attempt_id,
                        terminal_stream_will_publish=True,
                    ):
                        db_session.rollback()
                        raise ExecutionLeaseLost()
                    self._commit_terminal_settlement(
                        db_session,
                        event_writer,
                        self.event_service.execution_cancelled_events(execution),
                        execution=execution,
                    )
                    return
                if not matches_execution_attempt(
                    execution,
                    expected_worker_id=expected_worker_id,
                    expected_attempt_id=expected_attempt_id,
                ):
                    raise ExecutionLeaseLost()
                if (
                    execution.status == RunStatus.cancelled
                    or execution.cancel_requested_at is not None
                ):
                    if not settle_execution_cancellation(
                        db_session,
                        execution,
                        now=authorization_time,
                        expected_worker_id=expected_worker_id,
                        expected_attempt_id=expected_attempt_id,
                        terminal_stream_will_publish=True,
                    ):
                        db_session.rollback()
                        raise ExecutionLeaseLost()
                    self._commit_terminal_settlement(
                        db_session,
                        event_writer,
                        self.event_service.execution_cancelled_events(execution),
                        execution=execution,
                    )
                    return
                if not owns_active_execution_attempt(
                    execution,
                    expected_worker_id=expected_worker_id,
                    expected_attempt_id=expected_attempt_id,
                    now=authorization_time,
                ) or execution.status != RunStatus.running:
                    raise ExecutionLeaseLost()

                now = authorization_time
                if not finish_current_attempt(
                    db_session,
                    execution,
                    ExecutionAttemptStatus.waiting_input,
                    now=now,
                    expected_worker_id=expected_worker_id,
                    expected_attempt_id=expected_attempt_id,
                    require_active_lease=True,
                ):
                    raise ExecutionLeaseLost()
                execution.status = RunStatus.waiting_input
                execution.error = ""
                execution.interrupt_payload = result.interrupt_payload
                execution.worker_id = None
                execution.claimed_at = None
                execution.heartbeat_at = None
                execution.lease_expires_at = None
                execution.touch_updated_at(now)
                db_session.add(execution)
                mark_resume_consumed(db_session, resume_request_id)
                self._commit_terminal_settlement(
                    db_session,
                    event_writer,
                    self.event_service.waiting_input_events(
                        execution,
                        result.interrupt_payload,
                    ),
                    execution=execution,
                )
                return

            self._prepare_event_settlement(event_writer)
            locked_execution = db_session.exec(
                select(AgentExecution)
                .where(AgentExecution.id == execution.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one()
            execution = locked_execution
            authorization_time = current_database_time(db_session)
            if not matches_execution_attempt(
                execution,
                expected_worker_id=expected_worker_id,
                expected_attempt_id=expected_attempt_id,
            ):
                raise ExecutionLeaseLost()
            if (
                execution.status == RunStatus.cancelled
                or execution.cancel_requested_at is not None
            ):
                raise ExecutionCancelled()
            if not owns_active_execution_attempt(
                execution,
                expected_worker_id=expected_worker_id,
                expected_attempt_id=expected_attempt_id,
                now=authorization_time,
            ):
                raise ExecutionLeaseLost()
            if execution.status != RunStatus.running:
                raise ExecutionLeaseLost()
            now = authorization_time
            if not finish_current_attempt(
                db_session,
                execution,
                ExecutionAttemptStatus.completed,
                now=now,
                expected_worker_id=expected_worker_id,
                expected_attempt_id=expected_attempt_id,
                require_active_lease=True,
            ):
                raise ExecutionLeaseLost()
            execution.status = RunStatus.completed
            execution.error = ""
            execution.interrupt_payload = {}
            execution.finished_at = execution.finished_at or now
            execution.touch_updated_at(now)
            db_session.add(execution)
            mark_resume_consumed(db_session, resume_request_id)
            self.post_service.enqueue(
                db_session=db_session,
                execution=execution,
                request_id=request_id,
            )
            terminal_events = list(result.pending_events)
            if result.assistant_message is not None:
                assistant_delta = {
                        "execution_id": execution.id,
                        "message_id": result.assistant_message.id,
                        "message_type": result.assistant_message.message_type,
                        "chunk": (
                            ""
                            if result.streamed_assistant_text
                            else result.assistant_message.content
                        ),
                        "done": True,
                    }
                terminal_events.append(("assistant_message_delta", assistant_delta))
                assistant_message = {
                        "execution_id": execution.id,
                        "message_id": result.assistant_message.id,
                        "message_type": result.assistant_message.message_type,
                        "content": result.assistant_message.content,
                    }
                terminal_events.append(("assistant_message", assistant_message))
            terminal_events.extend(self.event_service.execution_completed_events(execution))
            self._commit_terminal_settlement(
                db_session,
                event_writer,
                terminal_events,
                execution=execution,
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
        except (ExecutionLeaseLost, CheckpointSupersededError):
            self._prepare_event_settlement(event_writer)
            db_session.rollback()
            logger.warning("Agent execution lease lost: %s", execution_id)
        except CheckpointOwnershipLostError:
            self._prepare_event_settlement(event_writer)
            db_session.rollback()
            cancelled_execution = self._finalize_cancellation(
                db_session,
                execution_id=execution_id,
                expected_worker_id=expected_worker_id,
                expected_attempt_id=expected_attempt_id,
            )
            if cancelled_execution is None:
                logger.warning("Agent execution lease lost: %s", execution_id)
            else:
                execution = cancelled_execution
                self._commit_terminal_settlement(
                    db_session,
                    event_writer,
                    self.event_service.execution_cancelled_events(execution),
                    execution=execution,
                )
        except ExecutionCancelled:
            self._prepare_event_settlement(event_writer)
            db_session.rollback()
            cancelled_execution = self._finalize_cancellation(
                db_session,
                execution_id=execution_id,
                expected_worker_id=expected_worker_id,
                expected_attempt_id=expected_attempt_id,
            )
            if cancelled_execution is not None:
                execution = cancelled_execution
                self._commit_terminal_settlement(
                    db_session,
                    event_writer,
                    self.event_service.execution_cancelled_events(execution),
                    execution=execution,
                )
        except Exception as exc:  # noqa: BLE001
            self._prepare_event_settlement(event_writer)
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
                expected_worker_id=expected_worker_id,
                expected_attempt_id=expected_attempt_id,
                user_id=auth.user_id,
                error=error_detail.message,
            )
            if settled_execution is not None:
                execution = settled_execution
                if execution.status == RunStatus.cancelled:
                    terminal_events = self.event_service.execution_cancelled_events(execution)
                else:
                    terminal_events = self.event_service.execution_failed_events(
                        execution,
                        error_detail.message,
                        error_code=error_detail.code,
                        retryable=error_detail.retryable,
                    )
                self._commit_terminal_settlement(
                    db_session,
                    event_writer,
                    terminal_events,
                    execution=execution,
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
        expected_attempt_id: str | None,
    ) -> AgentExecution | None:
        execution = db_session.exec(
            select(AgentExecution)
            .where(AgentExecution.id == execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if (
            execution is None
            or not matches_execution_attempt(
                execution,
                expected_worker_id=expected_worker_id,
                expected_attempt_id=expected_attempt_id,
            )
            or execution.status in {RunStatus.completed, RunStatus.failed}
            or (
                execution.status != RunStatus.cancelled
                and execution.cancel_requested_at is None
            )
        ):
            db_session.rollback()
            return None

        now = current_database_time(db_session)
        settled = settle_execution_cancellation(
            db_session,
            execution,
            now=now,
            expected_worker_id=expected_worker_id,
            expected_attempt_id=expected_attempt_id,
            terminal_stream_will_publish=True,
        )
        if not settled:
            db_session.rollback()
            return None
        return execution

    def _finalize_failure(
        self,
        db_session: Session,
        *,
        execution_id: str,
        expected_worker_id: str,
        expected_attempt_id: str | None,
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
            or not matches_execution_attempt(
                execution,
                expected_worker_id=expected_worker_id,
                expected_attempt_id=expected_attempt_id,
            )
            or execution.status not in {RunStatus.pending, RunStatus.running}
        ):
            db_session.rollback()
            return None

        now = current_database_time(db_session)
        if user is None or user.status != "active":
            settled = settle_execution_cancellation(
                db_session,
                execution,
                now=now,
                error="USER_DISABLED",
                expected_worker_id=expected_worker_id,
                expected_attempt_id=expected_attempt_id,
                terminal_stream_will_publish=True,
            )
        elif execution.cancel_requested_at is not None:
            settled = settle_execution_cancellation(
                db_session,
                execution,
                now=now,
                expected_worker_id=expected_worker_id,
                expected_attempt_id=expected_attempt_id,
                terminal_stream_will_publish=True,
            )
        else:
            settled = settle_execution_failure(
                db_session,
                execution,
                now=now,
                error=error,
                expected_worker_id=expected_worker_id,
                expected_attempt_id=expected_attempt_id,
                terminal_stream_will_publish=True,
            )
        if not settled:
            db_session.rollback()
            return None
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
            select(AgentExecution, AgentInvocation, ChatSession, ExecutionOutbox)
            .join(AgentInvocation, AgentExecution.invocation_id == AgentInvocation.id)
            .join(ChatSession, AgentInvocation.session_id == ChatSession.id)
            .join(
                ExecutionOutbox,
                (ExecutionOutbox.execution_id == AgentExecution.id)
                & (ExecutionOutbox.kind == "execute"),
            )
            .where(AgentExecution.id == execution_id)
            .where(ChatSession.user_id == auth.user_id)
        )
        if agent_filter is not None:
            statement = statement.where(ChatSession.agent_id.in_(agent_filter))
        if thread_id is not None:
            statement = statement.where(ChatSession.langgraph_thread_id == thread_id)
        lineage_row = db_session.exec(statement).first()
        if lineage_row is None:
            return None
        execution, invocation, chat, outbox = lineage_row
        payload = outbox.payload
        lineage = payload.get("lineage") if isinstance(payload, dict) else None
        message_id = lineage.get("message_id") if isinstance(lineage, dict) else None
        if (
            not isinstance(message_id, str)
            or not message_id
            or message_id != message_id.strip()
        ):
            return None
        user_message = db_session.exec(
            select(ChatMessage)
            .where(ChatMessage.id == message_id)
            .where(ChatMessage.session_id == chat.id)
            .where(ChatMessage.invocation_id == invocation.id)
            .where(ChatMessage.execution_id.is_(None))
            .where(ChatMessage.role == MessageRole.user)
        ).one_or_none()
        if user_message is None:
            return None
        return execution, invocation, chat, user_message

    @staticmethod
    def _prepare_event_settlement(event_writer: Any) -> bool:
        prepare = getattr(event_writer, "prepare_settlement", None)
        if not callable(prepare):
            prepare = getattr(event_writer, "prepare_completion", None)
        if not callable(prepare):
            return False
        try:
            prepare()
            return True
        except Exception:  # noqa: BLE001
            logger.warning("Failed to freeze execution event writer.", exc_info=True)
            return False

    @staticmethod
    def _drain_event_settlement(
        event_writer: Any,
        events: list[tuple[str, dict[str, Any]]],
        *,
        execution_id: str,
        db_session: Session | None = None,
    ) -> bool:
        drain = getattr(event_writer, "drain_settlement", None)
        if not callable(drain):
            drain = getattr(event_writer, "drain_completion", None)
        try:
            if callable(drain):
                drain(events, db_session=db_session)
            else:
                for event_name, payload in events:
                    event_writer.emit(event_name, payload)
            return True
        except Exception:  # noqa: BLE001
            logger.warning(
                "Execution terminal event drain failed: execution=%s",
                execution_id,
                exc_info=True,
            )
            return False

    def _commit_terminal_settlement(
        self,
        db_session: Session,
        event_writer: Any,
        events: list[tuple[str, dict[str, Any]]],
        *,
        execution: AgentExecution,
    ) -> None:
        drained = self._drain_event_settlement(
            event_writer,
            events,
            execution_id=execution.id,
            db_session=db_session,
        )
        terminal_is_durable = bool(
            execution.terminal_stream_sequence is not None
            and execution.terminal_stream_attempt_id == execution.current_attempt_id
            and execution.terminal_stream_status == execution.status.value
        )
        if (not drained or not terminal_is_durable) and not execution.streaming_degraded:
            apply_streaming_degradation_first_wins(
                execution,
                reason="TERMINAL_STREAM_NOT_PUBLISHED",
                now=current_database_time(db_session),
            )
            db_session.add(execution)
        db_session.commit()
