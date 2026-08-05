from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from threading import Event
from typing import Any, Protocol

from sqlalchemy import delete, func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from contentai.agent.context.assembler import ContextAssembler
from contentai.agent.context.window import TokenCounter
from contentai.agent.runtime.errors import (
    MODEL_STREAM_INTERRUPTED_CODE,
    MODEL_STREAM_INTERRUPTED_MESSAGE,
)
from contentai.agent.runtime.turn_context import (
    TurnContextSnapshotError,
    assemble_turn_context,
    build_turn_context_snapshot,
    fallback_turn_context,
    load_turn_prompt_inputs,
)
from contentai.core.security import AuthContext
from contentai.models.agent import AgentProfile, AgentVersion
from contentai.models.base import new_id
from contentai.models.chat import (
    AgentExecution,
    AgentInvocation,
    ChatMessage,
    ChatSession,
    CheckpointDeletionOutbox,
    ExecutionOutbox,
    ExecutionResumeRequest,
)
from contentai.models.enums import ExecutionAttemptKind, MessageRole, MessageType, RunStatus
from contentai.models.memory import MemoryRecord
from contentai.models.schemas import (
    AgentExecutionState,
    AgentMessageRequest,
    ChatExecutionResponse,
    ChatMessageResponse,
    ChatSessionDetail,
    ChatSessionListResponse,
    ChatSessionSummary,
    ChatUserMessageResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    ErrorDetail,
    MessageListRequest,
)
from contentai.models.user import AdminAuditLog, AppUser
from contentai.services.agent_service import AgentService
from contentai.services.errors import (
    ActiveExecutionExistsError,
    AgentNotFoundError,
    ChatSessionNotFoundError,
    CurrentInputTooLargeError,
    IdempotencyPayloadMismatchError,
    InvalidCursorError,
    InvalidStreamCursorError,
    MessageAlreadyExistsError,
    ResponseItemTooLargeError,
    RunInterruptStaleError,
    StreamingDegradedError,
    StreamReplayExpiredError,
    StreamReplayGapError,
)
from contentai.services.event_stream import (
    EventStreamUnavailable,
    InvalidStreamCursor,
    RedisEventStream,
    StreamEvent,
    StreamReplayExpired,
    StreamReplayGap,
)
from contentai.services.execution_lineage import ExecutionLineage
from contentai.services.execution_resume import interrupt_identity, public_interrupt
from contentai.services.execution_scope import ExecutionScopeGuard
from contentai.services.execution_settlement import (
    apply_streaming_degradation_first_wins,
    current_database_time,
    mark_streaming_degraded_first_wins,
    settle_execution_cancellation,
)
from contentai.services.model_configuration_service import (
    ModelConfigurationService,
    RuntimeModelConfiguration,
)
from contentai.services.pagination import (
    MAX_RESPONSE_BYTES,
    CursorSigner,
    apply_descending_cursor,
    decode_cursor,
    encode_cursor,
    fit_response_items,
    signer_from_settings,
)

logger = logging.getLogger(__name__)

ACTIVE_EXECUTION_STATUSES = {RunStatus.pending, RunStatus.running}
WAITING_EXECUTION_STATUSES = {RunStatus.waiting_input}
BUSY_EXECUTION_STATUSES = ACTIVE_EXECUTION_STATUSES | WAITING_EXECUTION_STATUSES
TERMINAL_EXECUTION_STATUSES = {
    RunStatus.completed,
    RunStatus.failed,
    RunStatus.cancelled,
}
STREAM_TERMINAL_STATUSES = TERMINAL_EXECUTION_STATUSES | {RunStatus.waiting_input}
_TERMINAL_STREAM_EVENT_NAME = {
    RunStatus.waiting_input: "run_interrupt",
    RunStatus.completed: "run_finish",
    RunStatus.failed: "run_error",
    RunStatus.cancelled: "run_cancel",
}


@dataclass(frozen=True)
class _ReplayFenceSnapshot:
    first_event_at: datetime | None
    streaming_degraded: bool
    streaming_degraded_reason: str
    status: RunStatus
    current_attempt_id: str | None
    stream_committed_sequence: int
    terminal_stream_sequence: int | None
    terminal_stream_attempt_id: str | None
    terminal_stream_status: str | None


def _stream_database_invariant_reason(execution: AgentExecution) -> str | None:
    """Return the stable sticky reason for an impossible durable stream state."""
    terminal_sequence = execution.terminal_stream_sequence
    if execution.status in STREAM_TERMINAL_STATUSES:
        if terminal_sequence is None:
            return "TERMINAL_STREAM_NOT_PUBLISHED"
        if (
            execution.terminal_stream_attempt_id != execution.current_attempt_id
            or execution.terminal_stream_status != execution.status.value
        ):
            return "STREAM_REPLAY_GAP"
        return None
    if terminal_sequence is not None and (
        execution.status not in ACTIVE_EXECUTION_STATUSES
        or execution.terminal_stream_status != RunStatus.waiting_input.value
    ):
        return "STREAM_REPLAY_GAP"
    return None


def _persist_streaming_degradation_locked_best_effort(
    session: Session,
    execution: AgentExecution,
    *,
    reason: str,
) -> None:
    """Keep the public replay error stable even if sticky persistence also fails."""
    try:
        now = current_database_time(session)
        apply_streaming_degradation_first_wins(execution, reason=reason, now=now)
        session.add(execution)
        session.commit()
    except Exception:  # noqa: BLE001
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to rollback stream degradation persistence: execution=%s",
                execution.id,
            )
        logger.exception(
            "Failed to persist stream degradation: execution=%s reason=%s",
            execution.id,
            reason,
        )

__all__ = ["ConversationService", "InvalidCursorError"]


class ExecutionDispatcher(Protocol):
    def dispatch(self, execution_id: str, request_id: str | None = None) -> None: ...


class ConversationService:
    def __init__(
        self,
        agent_service: AgentService,
        *,
        execution_dispatcher: ExecutionDispatcher | None = None,
    ) -> None:
        self.agent_service = agent_service
        self._cursor_signer = signer_from_settings(agent_service.settings)
        self.execution_dispatcher = execution_dispatcher
        self._execution_scope_guard = ExecutionScopeGuard()
        self._input_token_counter = TokenCounter()
        self._model_configurations = ModelConfigurationService(agent_service.settings)

    def close(self) -> None:
        return None

    def create_session(
        self,
        session: Session,
        payload: CreateSessionRequest,
        auth: AuthContext,
    ) -> CreateSessionResponse:
        if not auth.can_access_agent(payload.agent_id):
            raise AgentNotFoundError(payload.agent_id)
        profile = session.exec(
            select(AgentProfile)
            .where(AgentProfile.id == payload.agent_id)
            .where(AgentProfile.user_id == auth.user_id)
            .with_for_update()
        ).first()
        if profile is None:
            raise AgentNotFoundError(payload.agent_id)

        version = self._latest_version_for_agent(session, payload.agent_id)
        chat = ChatSession(
            agent_id=payload.agent_id,
            agent_version_id=version.id,
            user_id=auth.user_id,
        )
        session.add(chat)
        session.commit()
        session.refresh(chat)
        return CreateSessionResponse(
            session_id=chat.id,
            agent_id=chat.agent_id,
            agent_version_id=chat.agent_version_id,
            title=chat.title,
        )

    def list_sessions(
        self,
        session: Session,
        auth: AuthContext,
        *,
        agent_id: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> ChatSessionListResponse:
        if agent_id is not None and not auth.can_access_agent(agent_id):
            raise AgentNotFoundError(agent_id)
        statement = select(ChatSession).where(ChatSession.user_id == auth.user_id)
        if agent_id is not None:
            statement = statement.where(ChatSession.agent_id == agent_id)
        scope = f"chat.sessions:{auth.user_id}:{agent_id or ''}"
        statement = apply_descending_cursor(
            statement,
            ChatSession.updated_at,
            ChatSession.id,
            cursor,
            scope=scope,
            signer=self._cursor_signer,
        ).order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
        chats = list(session.exec(statement.limit(limit + 1)).all())
        has_more = len(chats) > limit
        chats = chats[:limit]
        if not chats:
            return ChatSessionListResponse(items=[], next_cursor=None)
        ids = [chat.id for chat in chats]
        count_rows = session.exec(
            select(ChatMessage.session_id, func.count(ChatMessage.id))
            .where(ChatMessage.session_id.in_(ids))
            .where(ChatMessage.message_type.in_((MessageType.text, MessageType.markdown)))
            .group_by(ChatMessage.session_id)
        ).all()
        counts = {row[0]: int(row[1] or 0) for row in count_rows}
        latest_rows = session.exec(
            select(AgentExecution.session_id, AgentExecution.status)
            .where(AgentExecution.session_id.in_(ids))
            .distinct(AgentExecution.session_id)
            .order_by(
                AgentExecution.session_id,
                AgentExecution.updated_at.desc(),
                AgentExecution.id.desc(),
            )
        ).all()
        latest = {row[0]: row[1] for row in latest_rows}
        items = [
            self._session_summary(
                chat,
                latest_execution_status=latest.get(chat.id),
                message_count=counts.get(chat.id, 0),
            )
            for chat in chats
        ]
        items, budget_more = fit_response_items(items)
        has_more = has_more or budget_more
        next_cursor = None
        if has_more and items:
            boundary = chats[len(items) - 1]
            next_cursor = encode_cursor(
                boundary.updated_at, boundary.id, scope=scope, signer=self._cursor_signer
            )
        return ChatSessionListResponse(items=items, next_cursor=next_cursor)

    def get_session(
        self,
        session: Session,
        session_id: str,
        auth: AuthContext,
        history: MessageListRequest | None = None,
    ) -> ChatSessionDetail:
        chat = self._get_chat(session, session_id, auth)
        latest_execution = self._get_latest_execution_for_session(session, chat.id)
        request = history or MessageListRequest()
        messages, next_cursor = self._get_session_messages(
            session,
            session_id=chat.id,
            request=request,
            scope=f"chat.messages:{auth.user_id}:{chat.id}",
        )
        message_count_result = session.exec(
            select(func.count())
            .where(ChatMessage.session_id == chat.id)
            .where(ChatMessage.message_type.in_((MessageType.text, MessageType.markdown)))
        ).one_or_none()
        message_count = 0 if message_count_result is None else int(message_count_result)

        return ChatSessionDetail(
            **self._session_summary(
                chat,
                latest_execution_status=latest_execution.status if latest_execution else None,
                message_count=int(message_count or 0),
            ).model_dump(),
            messages=[self._to_message_response(item) for item in messages],
            next_cursor=next_cursor,
            latest_execution=(
                self._execution_response(
                    execution=latest_execution,
                    chat_id=chat.id,
                    queue_stage=self._queue_stage(session, latest_execution),
                ).model_dump()
                if latest_execution
                else None
            ),
        )

    def delete_session(
        self,
        session: Session,
        session_id: str,
        auth: AuthContext,
        request_id: str = "",
    ) -> None:
        chat = self._get_chat(session, session_id, auth, for_update=True)
        executions = self._get_executions_for_session(session, chat.id)
        if any(execution.status in BUSY_EXECUTION_STATUSES for execution in executions):
            raise ActiveExecutionExistsError("Chat session has an active execution")

        outbox_rows = [
            CheckpointDeletionOutbox(
                user_id=auth.user_id,
                session_id=chat.id,
                thread_id=chat.langgraph_thread_id,
                checkpoint_ns=execution.id,
            )
            for execution in executions
        ]
        try:
            session.exec(delete(MemoryRecord).where(MemoryRecord.session_id == chat.id))
            session.delete(chat)
            for row in outbox_rows:
                session.add(row)
            session.add(
                AdminAuditLog(
                    actor_user_id=auth.user_id,
                    target_user_id=auth.user_id,
                    action="session.deleted",
                    request_id=request_id,
                    detail={
                        "session_hash": hashlib.sha256(chat.id.encode("utf-8")).hexdigest(),
                    },
                )
            )
            session.commit()
        except Exception:
            session.rollback()
            raise

    def create_turn(
        self,
        session: Session,
        payload: AgentMessageRequest,
        auth: AuthContext,
        *,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> tuple[ChatUserMessageResponse, bool]:
        lineage = ExecutionLineage.resolve_for_update(session, payload.session_id, auth)
        chat = lineage.chat
        normalized_key = self._normalize_idempotency_key(idempotency_key)
        request_sha256 = self._request_sha256(payload)
        if normalized_key is not None:
            replayed = self._find_replayed_turn(
                session,
                chat=chat,
                auth=auth,
                idempotency_key=normalized_key,
                request_sha256=request_sha256,
            )
            if replayed is not None:
                message, invocation, execution = replayed
                return ChatUserMessageResponse(
                    session_id=chat.id,
                    message_id=message.id,
                    execution_id=execution.id,
                    status=self._execution_state(execution.status),
                    trace_id=execution.trace_id,
                    error=self._execution_error(execution.error),
                ), True
        self._ensure_no_active_execution(session, chat.id, for_update=True)
        model_configuration = self._model_configurations.get_required_active_runtime(session)
        message_id = payload.message_id or new_id("msg")
        invocation_id = new_id("inv")
        execution_id = new_id("exe")
        turn_context_snapshot = self._ensure_current_input_fits(
            session,
            chat=chat,
            content=payload.message,
            message_id=message_id,
            invocation_id=invocation_id,
            execution_id=execution_id,
            auth=auth,
            model_configuration=model_configuration,
        )

        try:
            message, invocation, execution = self._create_user_message_invocation_execution(
                session,
                chat=chat,
                content=payload.message,
                message_id=message_id,
                invocation_id=invocation_id,
                execution_id=execution_id,
                turn_context_snapshot=turn_context_snapshot,
                auth=auth,
                idempotency_key=normalized_key,
                request_sha256=request_sha256,
                request_id=request_id,
                model_config_id=model_configuration.id,
            )
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            if self._integrity_constraint_name(exc) == "chatmessage_pkey":
                raise MessageAlreadyExistsError(
                    "The message_id is already in use."
                ) from exc
            raise
        except Exception:
            # A failed commit leaves a SQLAlchemy Session unusable until it is
            # rolled back. Preserve the original infrastructure error while
            # restoring the caller-owned session to a valid state.
            session.rollback()
            raise

        response = ChatUserMessageResponse(
            session_id=chat.id,
            message_id=message.id,
            execution_id=execution.id,
            status=self._execution_state(execution.status),
            trace_id=execution.trace_id,
            error=self._execution_error(execution.error),
        )
        if self.execution_dispatcher is not None:
            self.execution_dispatcher.dispatch(execution.id, request_id)
        return response, False

    @staticmethod
    def _integrity_constraint_name(exc: IntegrityError) -> str | None:
        diagnostic_name = getattr(
            getattr(exc.orig, "diag", None),
            "constraint_name",
            None,
        )
        if diagnostic_name:
            return str(diagnostic_name)
        message = str(exc)
        if "chatmessage_pkey" in message:
            return "chatmessage_pkey"
        if "UNIQUE constraint failed: chatmessage.id" in message:
            return "chatmessage_pkey"
        return None

    def _ensure_current_input_fits(
        self,
        session: Session,
        *,
        chat: ChatSession,
        content: str,
        message_id: str,
        invocation_id: str,
        execution_id: str,
        auth: AuthContext,
        model_configuration: RuntimeModelConfiguration,
    ) -> dict[str, Any]:
        settings = getattr(self.agent_service, "settings", None)
        from langchain_core.messages import HumanMessage, SystemMessage

        runtime = getattr(self.agent_service, "runtime", None)
        profile = session.get(AgentProfile, chat.agent_id)
        version = session.get(AgentVersion, chat.agent_version_id)
        if runtime is None or profile is None or version is None:
            prompt_inputs, context = fallback_turn_context(content=content)
            input_tokens = self._input_token_counter.count_messages([HumanMessage(content=content)])
        else:
            tools = runtime.get_tools(auth.tool_permissions)
            model_gateway = runtime.gateway_for_model_config(model_configuration.id)
            build_counter = getattr(model_gateway, "build_token_counter", None)
            token_counter = (
                build_counter(tools=tools)
                if callable(build_counter)
                else TokenCounter(tools=tools)
            )
            # Research does not exist durably until the turn has been created. The worker repeats
            # the complete check once an execution-local research package is available.
            prompt_inputs = load_turn_prompt_inputs(
                session,
                session_id=chat.id,
                user_id=auth.user_id,
                agent_id=chat.agent_id,
                focus_message=content,
                pending_message=HumanMessage(content=content, id=message_id),
            )
            tool_names = [str(getattr(tool, "name", "")) for tool in tools]
            context = assemble_turn_context(
                context_assembler=ContextAssembler(settings=settings),
                context_window_tokens=model_configuration.context_window_tokens,
                chat_max_tokens=model_configuration.chat_max_tokens,
                agent_profile=profile,
                agent_version=version,
                prompt_inputs=prompt_inputs,
                tool_names=tool_names,
                focus_message=content,
                user_id=auth.user_id,
                conversation_id=chat.id,
                execution_id=execution_id,
                research_package=None,
                token_counter=token_counter,
            )
            input_tokens = context.input_tokens
            if input_tokens is None:
                input_tokens = token_counter.count_messages(
                    [SystemMessage(content=context.system_prompt), *context.messages]
                )
        input_budget = (
            model_configuration.context_window_tokens - model_configuration.chat_max_tokens
        )
        if input_budget <= 0 or input_tokens > input_budget:
            raise CurrentInputTooLargeError(
                "Current input exceeds the model context budget."
            )
        try:
            return build_turn_context_snapshot(
                context=context,
                prompt_inputs=prompt_inputs,
                tool_permissions=auth.tool_permissions,
                role=auth.role,
                execution_id=execution_id,
                invocation_id=invocation_id,
                session_id=chat.id,
                user_id=auth.user_id,
                agent_id=chat.agent_id,
                agent_version_id=chat.agent_version_id,
                message_id=message_id,
                focus_message=content,
            )
        except TurnContextSnapshotError as exc:
            raise CurrentInputTooLargeError(
                "Current input exceeds the model context budget."
            ) from exc

    def submit_user_message_background(
        self,
        session: Session,
        payload: AgentMessageRequest,
        auth: AuthContext,
        idempotency_key: str | None = None,
        request_id: str | None = None,
    ) -> ChatUserMessageResponse:
        created, _is_replayed = self.create_turn(
            session,
            payload,
            auth,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
        return created

    def get_execution_status(
        self,
        session: Session,
        execution_id: str,
        auth: AuthContext,
    ) -> ChatExecutionResponse:
        execution, _, chat = self._get_execution_in_scope(
            session=session,
            execution_id=execution_id,
            auth=auth,
        )
        return self._execution_response(
            execution=execution,
            chat_id=chat.id,
            queue_stage=self._queue_stage(session, execution),
        )

    def cancel_execution(
        self,
        session: Session,
        execution_id: str,
        auth: AuthContext,
    ) -> ChatExecutionResponse:
        execution, _, chat = self._get_execution_in_scope(
            session=session,
            execution_id=execution_id,
            auth=auth,
        )
        execution = self._cancel_execution(session, execution)
        return self._execution_response(
            execution=execution,
            chat_id=chat.id,
            queue_stage=self._queue_stage(session, execution),
        )

    def resume_execution(
        self,
        session: Session,
        execution_id: str,
        interrupt_id: str,
        decision: str,
        auth: AuthContext,
        request_id: str | None = None,
    ) -> ChatExecutionResponse:
        execution, invocation, chat = self._get_execution_in_scope(
            session=session,
            execution_id=execution_id,
            auth=auth,
        )
        execution = self._resume_execution(
            session,
            execution,
            invocation=invocation,
            chat=chat,
            interrupt_id=interrupt_id,
            decision=decision,
        )
        response = self._execution_response(
            execution=execution,
            chat_id=chat.id,
            queue_stage=self._queue_stage(session, execution),
        )
        if self.execution_dispatcher is not None:
            self.execution_dispatcher.dispatch(execution.id, request_id)
        return response

    def replay_execution_events(
        self,
        session_bind: Any,
        execution_id: str,
        auth: AuthContext,
        *,
        after_sequence: int = 0,
        poll_interval_seconds: float = 0.35,
        stop_requested: Event | None = None,
        cursor_prevalidated: bool = False,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        """Replay ephemeral Redis execution events until the run reaches a terminal state."""
        cursor = max(0, after_sequence)
        with Session(session_bind) as session:
            scoped = self._execution_scope_guard.require_execution(
                session=session,
                execution_id=execution_id,
                auth=auth,
            )
            execution = scoped.execution
            snapshot = _ReplayFenceSnapshot(
                first_event_at=execution.first_event_at,
                streaming_degraded=execution.streaming_degraded,
                streaming_degraded_reason=execution.streaming_degraded_reason,
                status=execution.status,
                current_attempt_id=execution.current_attempt_id,
                stream_committed_sequence=int(execution.stream_committed_sequence),
                terminal_stream_sequence=execution.terminal_stream_sequence,
                terminal_stream_attempt_id=execution.terminal_stream_attempt_id,
                terminal_stream_status=execution.terminal_stream_status,
            )
        locked_snapshot = self._load_replay_snapshot(session_bind, execution_id)
        if locked_snapshot is None:
            return
        snapshot = locked_snapshot
        realtime_stream = RedisEventStream.from_settings(self.agent_service.settings)
        cursor_needs_validation = not cursor_prevalidated
        history_observed = False
        status_poll_interval = max(0.1, float(poll_interval_seconds))
        next_status_poll_at = time.monotonic() + status_poll_interval

        while True:
            if stop_requested is not None and stop_requested.is_set():
                return
            if snapshot.streaming_degraded:
                raise StreamingDegradedError(
                    snapshot.streaming_degraded_reason or "STREAMING_DEGRADED"
                )

            read_started_at = time.monotonic()
            try:
                realtime_rows = realtime_stream.read(
                    execution_id,
                    after_sequence=cursor,
                    first_event_at=snapshot.first_event_at,
                    history_observed=history_observed,
                    validate_cursor=cursor_needs_validation,
                )
                cursor_needs_validation = False
            except StreamReplayGap as exc:
                self._mark_streaming_degraded(session_bind, execution_id, "STREAM_REPLAY_GAP")
                raise StreamReplayGapError(str(exc)) from exc
            except StreamReplayExpired as exc:
                self._mark_streaming_degraded(session_bind, execution_id, "STREAM_REPLAY_EXPIRED")
                raise StreamReplayExpiredError(str(exc)) from exc
            except InvalidStreamCursor as exc:
                raise InvalidStreamCursorError(str(exc)) from exc
            except EventStreamUnavailable as exc:
                self._mark_streaming_degraded(session_bind, execution_id, "REDIS_READ_FAILED")
                raise StreamingDegradedError("REDIS_READ_FAILED") from exc
            if stop_requested is not None and stop_requested.is_set():
                return

            try:
                snapshot = self._validate_replay_visibility(
                    session_bind,
                    execution_id,
                    realtime_stream=realtime_stream,
                    rows=realtime_rows,
                    cursor=cursor,
                    history_observed=history_observed,
                )
            except StreamReplayGap as exc:
                raise StreamReplayGapError(str(exc)) from exc
            except StreamReplayExpired as exc:
                raise StreamReplayExpiredError(str(exc)) from exc
            except EventStreamUnavailable as exc:
                self._mark_streaming_degraded(
                    session_bind,
                    execution_id,
                    "REDIS_READ_FAILED",
                )
                raise StreamingDegradedError("REDIS_READ_FAILED") from exc
            if stop_requested is not None and stop_requested.is_set():
                return
            if snapshot.streaming_degraded:
                raise StreamingDegradedError(
                    snapshot.streaming_degraded_reason or "STREAMING_DEGRADED"
                )
            if realtime_rows:
                history_observed = True
            if not realtime_rows:
                if (
                    snapshot.status in STREAM_TERMINAL_STATUSES
                    and snapshot.terminal_stream_sequence is not None
                    and snapshot.terminal_stream_status == snapshot.status.value
                    and cursor >= snapshot.terminal_stream_sequence
                ):
                    return
                remaining_delay = status_poll_interval - (time.monotonic() - read_started_at)
                if remaining_delay > 0:
                    if stop_requested is None:
                        time.sleep(remaining_delay)
                    elif stop_requested.wait(remaining_delay):
                        return

            for realtime_event in realtime_rows:
                if stop_requested is not None and stop_requested.is_set():
                    return
                if realtime_event.sequence <= cursor:
                    continue
                cursor = realtime_event.sequence
                realtime_payload = dict(realtime_event.payload or {})
                realtime_payload["sequence"] = realtime_event.sequence
                realtime_payload["execution_id"] = execution_id
                if realtime_event.timestamp is not None:
                    realtime_payload.setdefault("timestamp", realtime_event.timestamp.isoformat())
                yield realtime_event.event_type, realtime_payload

            if (
                snapshot.status in STREAM_TERMINAL_STATUSES
                and snapshot.terminal_stream_sequence is not None
                and snapshot.terminal_stream_status == snapshot.status.value
                and cursor >= snapshot.terminal_stream_sequence
            ):
                return

            if stop_requested is not None and stop_requested.is_set():
                return
            now = time.monotonic()
            if now < next_status_poll_at:
                continue
            snapshot = self._load_replay_snapshot(session_bind, execution_id)
            if snapshot is None:
                return
            next_status_poll_at = now + status_poll_interval

    @staticmethod
    def _load_replay_snapshot(
        session_bind: Any,
        execution_id: str,
    ) -> _ReplayFenceSnapshot | None:
        with Session(session_bind) as session:
            execution = session.exec(
                select(AgentExecution)
                .where(AgentExecution.id == execution_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one_or_none()
            if execution is None:
                session.rollback()
                return None
            if not execution.streaming_degraded:
                invariant_reason = _stream_database_invariant_reason(execution)
                if invariant_reason is not None:
                    _persist_streaming_degradation_locked_best_effort(
                        session,
                        execution,
                        reason=invariant_reason,
                    )
                else:
                    session.rollback()
            else:
                session.rollback()
            return _ReplayFenceSnapshot(
                first_event_at=execution.first_event_at,
                streaming_degraded=execution.streaming_degraded,
                streaming_degraded_reason=execution.streaming_degraded_reason,
                status=execution.status,
                current_attempt_id=execution.current_attempt_id,
                stream_committed_sequence=int(execution.stream_committed_sequence),
                terminal_stream_sequence=execution.terminal_stream_sequence,
                terminal_stream_attempt_id=execution.terminal_stream_attempt_id,
                terminal_stream_status=execution.terminal_stream_status,
            )

    @staticmethod
    def _validate_replay_visibility(
        session_bind: Any,
        execution_id: str,
        *,
        realtime_stream: RedisEventStream,
        rows: list[StreamEvent],
        cursor: int,
        history_observed: bool,
    ) -> _ReplayFenceSnapshot:
        with Session(session_bind) as session:
            execution = session.exec(
                select(AgentExecution)
                .where(AgentExecution.id == execution_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one_or_none()
            if execution is None:
                raise StreamReplayExpired("Execution no longer exists")
            if execution.streaming_degraded:
                session.rollback()
                return _ReplayFenceSnapshot(
                    first_event_at=execution.first_event_at,
                    streaming_degraded=True,
                    streaming_degraded_reason=execution.streaming_degraded_reason,
                    status=execution.status,
                    current_attempt_id=execution.current_attempt_id,
                    stream_committed_sequence=int(execution.stream_committed_sequence),
                    terminal_stream_sequence=execution.terminal_stream_sequence,
                    terminal_stream_attempt_id=execution.terminal_stream_attempt_id,
                    terminal_stream_status=execution.terminal_stream_status,
                )

            invariant_reason = _stream_database_invariant_reason(execution)
            if invariant_reason is not None:
                _persist_streaming_degradation_locked_best_effort(
                    session,
                    execution,
                    reason=invariant_reason,
                )
                return _ReplayFenceSnapshot(
                    first_event_at=execution.first_event_at,
                    streaming_degraded=True,
                    streaming_degraded_reason=execution.streaming_degraded_reason,
                    status=execution.status,
                    current_attempt_id=execution.current_attempt_id,
                    stream_committed_sequence=int(execution.stream_committed_sequence),
                    terminal_stream_sequence=execution.terminal_stream_sequence,
                    terminal_stream_attempt_id=execution.terminal_stream_attempt_id,
                    terminal_stream_status=execution.terminal_stream_status,
                )

            committed_sequence = int(execution.stream_committed_sequence)
            batch_max = max((row.sequence for row in rows), default=cursor)
            if batch_max > committed_sequence:
                _persist_streaming_degradation_locked_best_effort(
                    session,
                    execution,
                    reason="STREAM_COMMIT_WATERMARK_MISMATCH",
                )
                raise StreamReplayGap(
                    "Redis event became visible before its database watermark committed"
                )

            expected_terminal_name = (
                _TERMINAL_STREAM_EVENT_NAME.get(RunStatus(execution.terminal_stream_status))
                if execution.terminal_stream_status is not None
                else None
            )
            try:
                realtime_stream.validate_cursor(
                    execution_id,
                    after_sequence=cursor,
                    first_event_at=execution.first_event_at,
                    history_observed=history_observed or bool(rows),
                    committed_sequence=committed_sequence,
                    terminal_sequence=execution.terminal_stream_sequence,
                    terminal_attempt_id=(
                        execution.terminal_stream_attempt_id
                        if execution.terminal_stream_sequence is not None
                        else None
                    ),
                    terminal_event_name=(
                        expected_terminal_name
                        if execution.terminal_stream_sequence is not None
                        else None
                    ),
                    terminal_status=execution.terminal_stream_status,
                    allow_prior_waiting_terminal=bool(
                        execution.status in ACTIVE_EXECUTION_STATUSES
                        and execution.terminal_stream_status == "waiting_input"
                    ),
                )
            except StreamReplayGap:
                _persist_streaming_degradation_locked_best_effort(
                    session,
                    execution,
                    reason="STREAM_REPLAY_GAP",
                )
                raise
            except StreamReplayExpired:
                _persist_streaming_degradation_locked_best_effort(
                    session,
                    execution,
                    reason="STREAM_REPLAY_EXPIRED",
                )
                raise

            if execution.first_event_at is None and committed_sequence > 0:
                event_timestamp = next(
                    (row.timestamp for row in rows if row.timestamp is not None),
                    None,
                )
                persistence_time = current_database_time(session)
                execution.first_event_at = event_timestamp or persistence_time
                execution.touch_updated_at(persistence_time)
                session.add(execution)
                session.commit()
            else:
                session.rollback()
            return _ReplayFenceSnapshot(
                first_event_at=execution.first_event_at,
                streaming_degraded=execution.streaming_degraded,
                streaming_degraded_reason=execution.streaming_degraded_reason,
                status=execution.status,
                current_attempt_id=execution.current_attempt_id,
                stream_committed_sequence=committed_sequence,
                terminal_stream_sequence=execution.terminal_stream_sequence,
                terminal_stream_attempt_id=execution.terminal_stream_attempt_id,
                terminal_stream_status=execution.terminal_stream_status,
            )

    def validate_execution_event_cursor(
        self,
        session: Session,
        execution_id: str,
        auth: AuthContext,
        *,
        after_sequence: int,
    ) -> None:
        """Validate the initial Redis cursor before HTTP response headers are sent."""
        scoped = self._execution_scope_guard.require_execution(
            session=session,
            execution_id=execution_id,
            auth=auth,
        )
        execution = session.exec(
            select(AgentExecution)
            .where(AgentExecution.id == scoped.execution.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        if execution.streaming_degraded:
            raise StreamingDegradedError(
                execution.streaming_degraded_reason or "STREAMING_DEGRADED"
            )
        invariant_reason = _stream_database_invariant_reason(execution)
        if invariant_reason is not None:
            _persist_streaming_degradation_locked_best_effort(
                session,
                execution,
                reason=invariant_reason,
            )
            raise StreamingDegradedError(invariant_reason)
        first_event_at = execution.first_event_at
        realtime_stream = RedisEventStream.from_settings(self.agent_service.settings)
        terminal_event_name = (
            _TERMINAL_STREAM_EVENT_NAME.get(RunStatus(execution.terminal_stream_status))
            if execution.terminal_stream_status is not None
            else None
        )
        try:
            realtime_stream.validate_cursor(
                execution_id,
                after_sequence=max(0, after_sequence),
                first_event_at=first_event_at,
                committed_sequence=int(execution.stream_committed_sequence),
                terminal_sequence=execution.terminal_stream_sequence,
                terminal_attempt_id=(
                    execution.terminal_stream_attempt_id
                    if execution.terminal_stream_sequence is not None
                    else None
                ),
                terminal_event_name=terminal_event_name,
                terminal_status=execution.terminal_stream_status,
                allow_prior_waiting_terminal=bool(
                    execution.status in ACTIVE_EXECUTION_STATUSES
                    and execution.terminal_stream_status == "waiting_input"
                ),
            )
        except StreamReplayGap as exc:
            _persist_streaming_degradation_locked_best_effort(
                session,
                execution,
                reason="STREAM_REPLAY_GAP",
            )
            raise StreamReplayGapError(str(exc)) from exc
        except StreamReplayExpired as exc:
            _persist_streaming_degradation_locked_best_effort(
                session,
                execution,
                reason="STREAM_REPLAY_EXPIRED",
            )
            raise StreamReplayExpiredError(str(exc)) from exc
        except InvalidStreamCursor as exc:
            session.rollback()
            raise InvalidStreamCursorError(str(exc)) from exc
        except EventStreamUnavailable as exc:
            _persist_streaming_degradation_locked_best_effort(
                session,
                execution,
                reason="REDIS_READ_FAILED",
            )
            raise StreamingDegradedError("REDIS_READ_FAILED") from exc
        session.rollback()

    def _get_execution_in_scope(
        self,
        session: Session,
        execution_id: str,
        auth: AuthContext,
    ) -> tuple[AgentExecution, AgentInvocation, ChatSession]:
        scoped = self._execution_scope_guard.require_execution(
            session=session,
            execution_id=execution_id,
            auth=auth,
        )
        return scoped.execution, scoped.invocation, scoped.chat

    def _get_chat(
        self,
        session: Session,
        session_id: str,
        auth: AuthContext,
        *,
        for_update: bool = False,
    ) -> ChatSession:
        query = select(ChatSession).where(ChatSession.id == session_id)
        if for_update:
            query = query.with_for_update()
        chat = session.exec(query).first()
        if (
            chat is None
            or chat.user_id != auth.user_id
            or not auth.can_access_agent(chat.agent_id)
        ):
            raise ChatSessionNotFoundError(session_id)
        return chat

    def _get_latest_execution_for_session(
        self,
        session: Session,
        session_id: str,
        *,
        for_update: bool = False,
    ) -> AgentExecution | None:
        query = (
            select(AgentExecution)
            .join(AgentInvocation, AgentExecution.invocation_id == AgentInvocation.id)
            .where(AgentInvocation.session_id == session_id)
            .order_by(AgentExecution.updated_at.desc(), AgentExecution.id.desc())
        )
        if for_update:
            query = query.with_for_update()
        return session.exec(query).first()

    def _get_executions_for_session(
        self,
        session: Session,
        session_id: str,
    ) -> list[AgentExecution]:
        query = (
            select(AgentExecution)
            .join(AgentInvocation, AgentExecution.invocation_id == AgentInvocation.id)
            .where(AgentInvocation.session_id == session_id)
            .order_by(AgentExecution.created_at.asc())
        )
        return list(session.exec(query).all())

    def _get_messages_for_invocation(
        self,
        session: Session,
        *,
        invocation_id: str,
    ) -> list[ChatMessage]:
        return list(
            session.exec(
                select(ChatMessage)
                .where(ChatMessage.invocation_id == invocation_id)
                .where(ChatMessage.role.in_([MessageRole.user, MessageRole.assistant]))
                .order_by(ChatMessage.created_at.asc())
            ).all()
        )

    def _ensure_no_active_execution(
        self,
        session: Session,
        session_id: str,
        *,
        for_update: bool = False,
    ) -> None:
        query = (
            select(AgentExecution)
            .join(AgentInvocation, AgentExecution.invocation_id == AgentInvocation.id)
            .where(AgentInvocation.session_id == session_id)
            .where(AgentExecution.status.in_(BUSY_EXECUTION_STATUSES))
        )
        if for_update:
            query = query.with_for_update()
        if session.exec(query).first() is not None:
            raise ActiveExecutionExistsError("Chat session already has an active execution")

    @staticmethod
    def _latest_version_for_agent(session: Session, agent_id: str) -> AgentVersion:
        version = session.exec(
            select(AgentVersion)
            .where(AgentVersion.agent_id == agent_id)
            .order_by(AgentVersion.version.desc(), AgentVersion.created_at.desc())
            .limit(1)
        ).first()
        if version is None:
            raise AgentNotFoundError(agent_id)
        return version

    def _create_user_message_invocation_execution(
        self,
        session: Session,
        *,
        chat: ChatSession,
        content: str,
        message_id: str | None = None,
        invocation_id: str | None = None,
        execution_id: str | None = None,
        turn_context_snapshot: dict[str, Any] | None = None,
        auth: AuthContext,
        idempotency_key: str | None = None,
        request_sha256: str | None = None,
        request_id: str | None = None,
        model_config_id: str,
    ) -> tuple[ChatMessage, AgentInvocation, AgentExecution]:
        invocation = AgentInvocation(
            **({"id": invocation_id} if invocation_id else {}),
            session_id=chat.id,
            agent_id=chat.agent_id,
            user_id=auth.user_id,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )
        session.add(invocation)
        session.flush()

        message_kwargs: dict[str, Any] = {}
        if message_id:
            message_kwargs["id"] = message_id
        message = ChatMessage(
            **message_kwargs,
            session_id=chat.id,
            invocation_id=invocation.id,
            role=MessageRole.user,
            message_type=MessageType.text,
            content=content,
        )
        session.add(message)
        session.flush()

        execution = AgentExecution(
            **({"id": execution_id} if execution_id else {}),
            invocation_id=invocation.id,
            session_id=chat.id,
            agent_version_id=chat.agent_version_id,
            model_config_id=model_config_id,
        )
        session.add(execution)
        session.flush()
        session.add(
            ExecutionOutbox(
                execution_id=execution.id,
                model_config_id=execution.model_config_id,
                kind="execute",
                request_id=request_id or "",
                payload=turn_context_snapshot or {},
            )
        )

        chat.touch_updated_at()
        session.add(chat)
        return message, invocation, execution

    @staticmethod
    def _mark_streaming_degraded(session_bind: Any, execution_id: str, reason: str) -> None:
        """Sticky, independent transaction used by SSE replay failure paths."""
        try:
            mark_streaming_degraded_first_wins(
                session_bind,
                execution_id,
                reason,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Sticky streaming degradation persistence failed")

    def _find_replayed_turn(
        self,
        session: Session,
        *,
        chat: ChatSession,
        auth: AuthContext,
        idempotency_key: str,
        request_sha256: str,
    ) -> tuple[ChatMessage, AgentInvocation, AgentExecution] | None:
        rows = list(
            session.exec(
                select(ChatMessage, AgentInvocation, AgentExecution)
                .join(AgentInvocation, ChatMessage.invocation_id == AgentInvocation.id)
                .join(AgentExecution, AgentExecution.invocation_id == AgentInvocation.id)
                .where(ChatMessage.session_id == chat.id)
                .where(AgentInvocation.session_id == chat.id)
                .where(ChatMessage.role == MessageRole.user)
                .where(ChatMessage.execution_id.is_(None))
                .where(AgentInvocation.agent_id == chat.agent_id)
                .where(AgentInvocation.user_id == auth.user_id)
                .where(AgentInvocation.idempotency_key == idempotency_key)
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            ).all()
        )
        if not rows:
            return None
        message, invocation, execution = rows[0]
        if invocation.request_sha256 != request_sha256:
            raise IdempotencyPayloadMismatchError(
                "The idempotency key is already bound to a different request payload."
            )
        return message, invocation, execution

    @staticmethod
    def _request_sha256(payload: AgentMessageRequest) -> str:
        canonical = {
            "message": payload.message,
            "message_id": payload.message_id,
            "session_id": payload.session_id,
        }
        encoded = json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _normalize_idempotency_key(value: str | None) -> str | None:
        if not value:
            return None
        value = value.strip()
        return value or None

    def _cancel_execution(self, session: Session, execution: AgentExecution) -> AgentExecution:
        locked = session.exec(
            select(AgentExecution)
            .where(AgentExecution.id == execution.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if locked is None or locked.status in TERMINAL_EXECUTION_STATUSES:
            return locked or execution
        now = current_database_time(session)
        if locked.status in {RunStatus.pending, RunStatus.waiting_input}:
            settle_execution_cancellation(session, locked, now=now)
        else:
            locked.cancel_requested_at = locked.cancel_requested_at or now
            locked.touch_updated_at(now)
            session.add(locked)
        session.commit()
        session.refresh(locked)
        return locked

    def _resume_execution(
        self,
        session: Session,
        execution: AgentExecution,
        *,
        invocation: AgentInvocation,
        chat: ChatSession,
        interrupt_id: str,
        decision: str,
    ) -> AgentExecution:
        user = session.exec(
            select(AppUser)
            .where(AppUser.id == invocation.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if user is None or user.status != "active":
            raise RunInterruptStaleError("The run interrupt is stale.")
        locked = session.exec(
            select(AgentExecution)
            .where(AgentExecution.id == execution.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if locked is None or locked.status != RunStatus.waiting_input:
            raise RunInterruptStaleError("The run interrupt is stale.")
        safe_interrupt = public_interrupt(locked.interrupt_payload)
        if safe_interrupt is None or safe_interrupt.interrupt_id != interrupt_id:
            raise RunInterruptStaleError("The run interrupt is stale.")
        pending_interrupt_id, tool_calls_hash = interrupt_identity(locked.interrupt_payload)
        if not pending_interrupt_id or interrupt_id != pending_interrupt_id:
            raise RunInterruptStaleError("The run interrupt is stale.")
        if decision not in {"approve", "reject"}:
            raise RunInterruptStaleError("The run interrupt decision is invalid.")
        pending_resume = session.exec(
            select(ExecutionResumeRequest)
            .where(ExecutionResumeRequest.execution_id == locked.id)
            .where(ExecutionResumeRequest.interrupt_id == interrupt_id)
            .with_for_update()
        ).first()
        if pending_resume is not None:
            raise RunInterruptStaleError("The run interrupt is stale.")
        decision_message = ChatMessage(
            session_id=chat.id,
            invocation_id=invocation.id,
            execution_id=locked.id,
            role=MessageRole.user,
            message_type=MessageType.text,
            content=(
                "已批准工具执行。" if decision == "approve" else "已拒绝工具执行。"
            ),
        )
        session.add(decision_message)
        session.flush()
        session.add(
            ExecutionResumeRequest(
                execution_id=locked.id,
                interrupt_id=interrupt_id,
                decision=decision,
                message_id=decision_message.id,
                tool_calls_hash=tool_calls_hash,
                value={"decision": decision},
            )
        )
        resume_time = current_database_time(session)
        locked.status = RunStatus.pending
        locked.error = ""
        locked.finished_at = None
        locked.cancel_requested_at = None
        locked.interrupt_payload = {}
        locked.resume_payload = {"interrupt_id": interrupt_id, "decision": decision}
        locked.worker_id = None
        locked.claimed_at = None
        locked.heartbeat_at = None
        locked.lease_expires_at = None
        locked.next_attempt_kind = ExecutionAttemptKind.resume
        locked.touch_updated_at(resume_time)
        session.add(locked)
        outbox = session.exec(
            select(ExecutionOutbox).where(
                ExecutionOutbox.execution_id == locked.id,
                ExecutionOutbox.kind == "execute",
            )
        ).first()
        if outbox is None:
            locked.status = RunStatus.failed
            locked.error = "TURN_CONTEXT_SNAPSHOT_INVALID"
            locked.finished_at = resume_time
            apply_streaming_degradation_first_wins(
                locked,
                reason="TERMINAL_STREAM_NOT_PUBLISHED",
                now=resume_time,
            )
            session.add(locked)
            session.commit()
            session.refresh(locked)
            return locked
        outbox.status = "pending"
        outbox.available_at = locked.updated_at
        outbox.locked_by = None
        outbox.locked_until = None
        outbox.updated_at = locked.updated_at
        session.add(outbox)
        session.commit()
        session.refresh(locked)
        return locked

    def _get_session_messages(
        self,
        session: Session,
        *,
        session_id: str,
        request: MessageListRequest,
        scope: str | None = None,
    ) -> tuple[list[ChatMessage], str | None]:
        statement = (
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .where(ChatMessage.role.in_([MessageRole.user, MessageRole.assistant]))
        )
        scope = scope or f"chat.messages:{session_id}"
        statement = apply_descending_cursor(
            statement,
            ChatMessage.created_at,
            ChatMessage.id,
            request.cursor,
            scope=scope,
            signer=self._cursor_signer,
        ).order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        rows = list(session.exec(statement.limit(request.limit + 1)).all())
        if not rows:
            return [], None

        has_more = len(rows) > request.limit
        rows = rows[: request.limit]
        if rows and len(rows[0].content.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise ResponseItemTooLargeError("A message exceeds the 1 MiB response budget")
        items = [self._to_message_response(item) for item in rows]
        items, budget_more = fit_response_items(items)
        if rows and not items:
            raise ResponseItemTooLargeError("A message exceeds the 1 MiB response budget")
        rows = rows[: len(items)]
        rows.sort(key=lambda item: (item.created_at, item.id))

        next_cursor = None
        if has_more or budget_more:
            oldest = rows[0]
            next_cursor = encode_cursor(
                oldest.created_at, oldest.id, scope=scope, signer=self._cursor_signer
            )
        return rows, next_cursor

    @staticmethod
    def _encode_cursor(
        created_at: datetime,
        message_id: str,
        *,
        scope: str = "",
        signer: CursorSigner | None = None,
    ) -> str:
        return encode_cursor(created_at, message_id, scope=scope, signer=signer)

    @staticmethod
    def _decode_cursor(
        cursor: str | None,
        *,
        scope: str = "",
        signer: CursorSigner | None = None,
    ) -> tuple[datetime, str] | None:
        return decode_cursor(cursor, scope=scope, signer=signer)

    @staticmethod
    def _session_summary(
        chat: ChatSession,
        *,
        latest_execution_status: RunStatus | None,
        message_count: int = 0,
    ) -> ChatSessionSummary:
        return ChatSessionSummary(
            session_id=chat.id,
            agent_id=chat.agent_id,
            agent_version_id=chat.agent_version_id,
            title=chat.title,
            created_at=chat.created_at,
            updated_at=chat.updated_at,
            latest_execution_status=ConversationService._execution_state(latest_execution_status)
            if latest_execution_status is not None
            else None,
            message_count=message_count,
        )

    @staticmethod
    def _execution_response(
        *,
        execution: AgentExecution,
        chat_id: str,
        queue_stage: str | None = None,
    ) -> ChatExecutionResponse:
        return ChatExecutionResponse(
            id=execution.id,
            session_id=chat_id,
            status=ConversationService._execution_state(execution.status),
            trace_id=execution.trace_id,
            error=ConversationService._execution_error(execution.error),
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            cancel_requested_at=execution.cancel_requested_at,
            interrupt=public_interrupt(execution.interrupt_payload),
            streaming_degraded=execution.streaming_degraded,
            streaming_degraded_reason=execution.streaming_degraded_reason or "",
            queue_stage=queue_stage,
        )

    @staticmethod
    def _queue_stage(session: Session, execution: AgentExecution) -> str | None:
        if execution.status != RunStatus.pending:
            return None
        if execution.claimed_at is not None:
            return "starting"
        outbox = session.exec(
            select(ExecutionOutbox)
            .where(ExecutionOutbox.execution_id == execution.id)
            .where(ExecutionOutbox.kind == "execute")
        ).first()
        if outbox is not None and outbox.status == "published":
            return "waiting_worker"
        return "dispatching"

    @staticmethod
    def _to_message_response(item: ChatMessage) -> ChatMessageResponse:
        return ChatMessageResponse(
            id=item.id,
            role=item.role,
            message_type=item.message_type,
            content=item.content,
            created_at=item.created_at,
        )

    @staticmethod
    def _execution_state(value: RunStatus | str) -> AgentExecutionState:
        raw_value = value.value if isinstance(value, RunStatus) else str(value)
        try:
            return AgentExecutionState(raw_value)
        except ValueError as exc:
            raise ValueError(f"Unknown execution status: {raw_value}") from exc

    @staticmethod
    def _execution_error(value: Any) -> ErrorDetail | None:
        if value is None or value == "":
            return None
        if isinstance(value, ErrorDetail):
            return value
        if isinstance(value, dict):
            code = value.get("code", "EXECUTION_ERROR")
            message = value.get("message", "")
            retryable = bool(value.get("retryable", False))
            msg = str(message).strip()
            if not msg:
                return None
            return ErrorDetail(
                code=str(code).strip() or "EXECUTION_ERROR",
                message=msg,
                retryable=retryable,
            )
        msg = str(value).strip()
        if not msg:
            return None
        if msg == MODEL_STREAM_INTERRUPTED_MESSAGE:
            return ErrorDetail(
                code=MODEL_STREAM_INTERRUPTED_CODE,
                message=msg,
                retryable=True,
            )
        return ErrorDetail(code="EXECUTION_ERROR", message=msg, retryable=False)


__all__ = ["ConversationService"]
