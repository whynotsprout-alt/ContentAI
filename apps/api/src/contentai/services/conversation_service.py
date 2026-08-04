from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import delete, func
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
    StreamReplayExpired,
    StreamReplayGap,
)
from contentai.services.execution_lineage import ExecutionLineage
from contentai.services.execution_resume import interrupt_identity, public_interrupt
from contentai.services.execution_scope import ExecutionScopeGuard
from contentai.services.execution_settlement import settle_execution_cancellation
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

ACTIVE_EXECUTION_STATUSES = {RunStatus.pending, RunStatus.running}
WAITING_EXECUTION_STATUSES = {RunStatus.waiting_input}
BUSY_EXECUTION_STATUSES = ACTIVE_EXECUTION_STATUSES | WAITING_EXECUTION_STATUSES
TERMINAL_EXECUTION_STATUSES = {
    RunStatus.completed,
    RunStatus.failed,
    RunStatus.cancelled,
}

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
        profile = session.get(AgentProfile, payload.agent_id)
        if (
            profile is None or profile.user_id != auth.user_id
        ):
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
        try:
            session.commit()
        except Exception as exc:
            session.rollback()
            raise ActiveExecutionExistsError(
                "Chat session already has an active execution"
            ) from exc

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
            first_event_at = execution.first_event_at
            streaming_degraded = execution.streaming_degraded
            streaming_degraded_reason = execution.streaming_degraded_reason
        realtime_stream = RedisEventStream.from_settings(self.agent_service.settings)
        cursor_needs_validation = True
        status_poll_interval = max(0.1, float(poll_interval_seconds))
        next_status_poll_at = time.monotonic() + status_poll_interval

        while True:
            if streaming_degraded:
                raise StreamingDegradedError(streaming_degraded_reason or "STREAMING_DEGRADED")

            read_started_at = time.monotonic()
            try:
                realtime_rows = realtime_stream.read(
                    execution_id,
                    after_sequence=cursor,
                    first_event_at=first_event_at,
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
            if not realtime_rows:
                remaining_delay = status_poll_interval - (time.monotonic() - read_started_at)
                if remaining_delay > 0:
                    time.sleep(remaining_delay)

            terminal_event_seen = False
            for realtime_event in realtime_rows:
                if realtime_event.sequence <= cursor:
                    continue
                cursor = realtime_event.sequence
                realtime_payload = dict(realtime_event.payload or {})
                realtime_payload["sequence"] = realtime_event.sequence
                realtime_payload.setdefault("execution_id", execution_id)
                if realtime_event.timestamp is not None:
                    realtime_payload.setdefault("timestamp", realtime_event.timestamp.isoformat())
                yield realtime_event.event_type, realtime_payload
                terminal_event_seen = terminal_event_seen or realtime_event.event_type in {
                    "done",
                    "error",
                }

            if terminal_event_seen:
                return

            now = time.monotonic()
            if now < next_status_poll_at:
                continue
            with Session(session_bind) as session:
                execution = session.get(AgentExecution, execution_id)
                if execution is None:
                    return
                streaming_degraded = execution.streaming_degraded
                streaming_degraded_reason = execution.streaming_degraded_reason
                if execution.status == RunStatus.failed:
                    return
                if execution.status in TERMINAL_EXECUTION_STATUSES:
                    return
            next_status_poll_at = now + status_poll_interval

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
        with Session(session_bind) as session:
            execution = session.get(AgentExecution, execution_id)
            if execution is None or execution.streaming_degraded:
                return
            now = datetime.now().astimezone()
            execution.streaming_degraded = True
            execution.streaming_degraded_at = now
            execution.streaming_degraded_reason = reason[:500]
            execution.touch_updated_at(now)
            session.add(execution)
            session.commit()

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
        from contentai.models.base import utcnow

        now = utcnow()
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
        locked.touch_updated_at()
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
            locked.finished_at = locked.updated_at
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
