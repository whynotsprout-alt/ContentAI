from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Iterator
from typing import Any

from agent.runtime.events import AgentEventWriter
from agent.runtime.runner import AgentRunner
from core.security import AuthContext
from memory import LongTermMemory, MemoryRepository, ShortTermMemory
from models.account import Account
from models.chat import AgentExecution, AgentInvocation, ChatMessage, ChatSession, ToolExecution
from models.enums import MemoryScope, MessageRole, MessageType, RunStatus
from models.memory import MemoryRecord
from models.schemas import (
    ChatExecutionResponse,
    ChatMessageResponse,
    ChatSessionDetail,
    ChatSessionSummary,
    ChatUserMessageResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    ExecutionResponse,
    MessageCitation,
    MessageListRequest,
    MessageState,
    ToolExecutionResult,
    ChatHistoryResponse,
    ErrorDetail,
    AgentMessageRequest,
    ConversationMemory,
    UserReplyRequest,
)
from models.schemas import ChatRequest
from services.errors import (
    ActiveExecutionExistsError,
    AccountNotFoundError,
    ChatSessionNotFoundError,
    ExecutionNotFoundError,
    ExecutionNotResumableError,
    ExecutionResumeValueRequiredError,
    InvalidCursorError,
    AgentError,
)
from sqlmodel import Session, delete, select
from sqlalchemy import and_, or_, func

from agent.runtime.checkpoint import clear_thread_persistence

from datetime import datetime

ACTIVE_EXECUTION_STATUSES = {RunStatus.running, RunStatus.interrupted}
BUSY_EXECUTION_STATUSES = ACTIVE_EXECUTION_STATUSES
TERMINAL_EXECUTION_STATUSES = {
    RunStatus.completed,
    RunStatus.failed,
    RunStatus.cancelled,
    RunStatus.interrupted,
}

LOCAL_AUTH = AuthContext(user_id="local-user", tenant_id="local")
_MESSAGE_CURSOR_SEP = "|"
MAX_STREAM_QUEUE_SIZE = 256


class StreamingAgentEventWriter(AgentEventWriter):
    def __init__(self, execution_id: str, event_queue: queue.Queue[tuple[str, dict[str, Any]] | object]) -> None:
        super().__init__(execution_id)
        self.event_queue = event_queue

    def _write_event(self, event: str, data: dict[str, Any]) -> None:
        payload = data.copy() if isinstance(data, dict) else {}
        try:
            self.event_queue.put((event, payload), timeout=0.5)
        except queue.Full:
            self._replace_last((event, payload))

    def _replace_last(self, item: tuple[str, dict[str, Any]]) -> None:
        try:
            self.event_queue.get_nowait()
        except queue.Empty:
            return
        try:
            self.event_queue.put_nowait(item)
        except queue.Full:
            return


logger = logging.getLogger(__name__)


class ConversationService:
    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.runner = AgentRunner(runtime)
        self._threads: set[threading.Thread] = set()
        self._threads_lock = threading.Lock()
        self._closing = False

    def close(self) -> None:
        with self._threads_lock:
            self._closing = True
            threads = list(self._threads)
        for thread in threads:
            if thread.is_alive():
                thread.join(timeout=5)

    def create_session(
        self,
        session: Session,
        payload: CreateSessionRequest,
        auth: AuthContext = LOCAL_AUTH,
    ) -> CreateSessionResponse:
        if not auth.can_access_account(payload.account_id):
            raise AccountNotFoundError(payload.account_id)
        if session.get(Account, payload.account_id) is None:
            raise AccountNotFoundError(payload.account_id)

        chat = ChatSession(
            account_id=payload.account_id,
            tenant_id=auth.tenant_id,
            owner_user_id=auth.user_id,
        )
        session.add(chat)
        session.commit()
        session.refresh(chat)
        return CreateSessionResponse(
            session_id=chat.id,
            account_id=chat.account_id,
            tenant_id=chat.tenant_id,
            user_id=chat.owner_user_id,
            title=chat.title,
        )

    def list_sessions(self, session: Session, auth: AuthContext = LOCAL_AUTH) -> list[ChatSessionSummary]:
        latest_execution_status = (
            select(
                AgentInvocation.session_id.label("session_id"),
                AgentExecution.status.label("status"),
                func.row_number()
                .over(
                    partition_by=AgentInvocation.session_id,
                    order_by=(AgentExecution.updated_at.desc(), AgentExecution.id.desc()),
                )
                .label("rank"),
            )
            .join(AgentInvocation, AgentExecution.invocation_id == AgentInvocation.id)
            .subquery()
        )
        message_counts = (
            select(
                ChatMessage.session_id.label("session_id"),
                func.count(ChatMessage.id).label("message_count"),
            )
            .group_by(ChatMessage.session_id)
            .subquery()
        )

        rows = session.exec(
            select(
                ChatSession,
                latest_execution_status.c.status,
                func.coalesce(message_counts.c.message_count, 0).label("message_count"),
            )
            .outerjoin(
                latest_execution_status,
                (latest_execution_status.c.session_id == ChatSession.id)
                & (latest_execution_status.c.rank == 1),
            )
            .outerjoin(message_counts, message_counts.c.session_id == ChatSession.id)
            .where(ChatSession.tenant_id == auth.tenant_id)
            .where(ChatSession.owner_user_id == auth.user_id)
            .order_by(ChatSession.updated_at.desc(), ChatSession.id.desc())
        ).all()

        return [
            ChatSessionSummary(
                session_id=chat.id,
                account_id=chat.account_id,
                title=chat.title,
                created_at=chat.created_at,
                updated_at=chat.updated_at,
                latest_status=execution_status,
                status=execution_status,
                message_count=int(message_count or 0),
            )
            for chat, execution_status, message_count in rows
        ]

    def get_session(
        self,
        session: Session,
        session_id: str,
        auth: AuthContext = LOCAL_AUTH,
        history: MessageListRequest | None = None,
    ) -> ChatSessionDetail:
        chat = self._get_chat(session, session_id, auth)
        latest_execution = self._get_latest_execution_for_session(session, chat.id)
        request = history or MessageListRequest()
        messages, next_cursor = self._get_session_messages(
            session,
            session_id=chat.id,
            request=request,
        )
        message_count_result = session.exec(
            select(func.count()).where(ChatMessage.session_id == chat.id)
        ).one_or_none()
        message_count = 0 if message_count_result is None else int(message_count_result)

        return ChatSessionDetail(
            **self._session_summary(
                chat,
                latest_status=latest_execution.status if latest_execution else None,
                message_count=int(message_count or 0),
            ).model_dump(),
            messages=[self._to_message_response(item) for item in messages],
            next_cursor=next_cursor,
            latest_execution=(
                self._execution_response(execution=latest_execution, chat_id=chat.id).model_dump()
                if latest_execution
                else None
            ),
            memory=self._conversation_memory(
                session,
                runtime=self.runtime,
                chat=chat,
            ),
        )

    def delete_session(
        self,
        session: Session,
        session_id: str,
        auth: AuthContext = LOCAL_AUTH,
    ) -> None:
        chat = self._get_chat(session, session_id, auth, for_update=True)
        executions = self._get_executions_for_session(session, chat.id)
        if any(execution.status in BUSY_EXECUTION_STATUSES for execution in executions):
            raise ActiveExecutionExistsError("Chat session has an active execution")

        execution_ids = [execution.id for execution in executions]
        invocation_ids = [execution.invocation_id for execution in executions]
        try:
            if invocation_ids:
                for invocation in session.exec(
                    select(AgentInvocation).where(AgentInvocation.id.in_(invocation_ids))
                ).all():
                    invocation.user_message_id = None
                    session.add(invocation)
                session.flush()
            if execution_ids:
                session.exec(
                    delete(ToolExecution).where(ToolExecution.execution_id.in_(execution_ids))
                )
                session.exec(delete(AgentExecution).where(AgentExecution.id.in_(execution_ids)))
            session.exec(delete(ChatMessage).where(ChatMessage.session_id == chat.id))
            if invocation_ids:
                session.exec(delete(AgentInvocation).where(AgentInvocation.id.in_(invocation_ids)))
            session.exec(delete(MemoryRecord).where(MemoryRecord.session_id == chat.id))
            session.exec(
                delete(MemoryRecord).where(
                    MemoryRecord.tenant_id == chat.tenant_id,
                    MemoryRecord.user_id == chat.owner_user_id,
                )
            )
            try:
                clear_thread_persistence(thread_id=chat.langgraph_thread_id, session=session)
            except Exception:
                session.rollback()
                raise RuntimeError("Chat session persistence cleanup failed")
            session.delete(chat)
            session.commit()
        except Exception:
            session.rollback()
            raise

    def create_turn(
        self,
        session: Session,
        payload: AgentMessageRequest,
        auth: AuthContext = LOCAL_AUTH,
    ) -> ChatUserMessageResponse:
        chat = self._get_chat(session, payload.session_id, auth, for_update=True)
        self._ensure_no_active_execution(session, chat.id, for_update=True)

        message, invocation, execution = self._create_user_message_invocation_execution(
            session,
            chat=chat,
            content=payload.message,
            auth=auth,
        )
        try:
            session.commit()
        except Exception as exc:
            session.rollback()
            raise ActiveExecutionExistsError("Chat session already has an active execution") from exc

        return ChatUserMessageResponse(
            session_id=chat.id,
            message_id=message.id,
            status=execution.status,
            trace_id=execution.id,
            error=execution.error,
        )

    def create_and_run_stream(
        self,
        session: Session,
        payload: AgentMessageRequest,
        auth: AuthContext = LOCAL_AUTH,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        created = self.create_turn(session, payload, auth)
        if created.trace_id is None:
            raise RuntimeError("Missing trace id for execution")
        return self._stream_execution(
            session_bind=session.get_bind(),
            execution_id=created.trace_id,
            tool_permissions=auth.tool_permissions,
        )

    def create_and_run_background(
        self,
        session: Session,
        payload: AgentMessageRequest,
        auth: AuthContext = LOCAL_AUTH,
    ) -> ChatUserMessageResponse:
        created = self.create_turn(session, payload, auth)
        if created.trace_id is None:
            raise RuntimeError("Missing trace id for execution")
        self._run_background(
            session_bind=session.get_bind(),
            execution_id=created.trace_id,
            tool_permissions=auth.tool_permissions,
        )
        return created

    def cancel_session(
        self,
        session: Session,
        session_id: str,
        auth: AuthContext = LOCAL_AUTH,
    ) -> ChatSessionDetail:
        chat = self._get_chat(session, session_id, auth)
        execution = self._get_latest_execution_for_session(session, chat.id)
        if execution is not None:
            self._cancel_execution(session, execution.id)
        return self.get_session(session, session_id, auth)

    def resume_session(
        self,
        session: Session,
        session_id: str,
        payload: UserReplyRequest,
        auth: AuthContext = LOCAL_AUTH,
    ) -> ChatSessionDetail:
        chat = self._get_chat(session, session_id, auth, for_update=True)
        execution = self._get_latest_execution_for_session(session, chat.id, for_update=True)
        if execution is None:
            raise ExecutionNotResumableError("No execution found for this session")
        resume_value = self._resume_value(payload)
        self._resume_execution(session, execution.id, resume_value)
        return self.get_session(session, session_id, auth)

    def resume_and_run_stream(
        self,
        session: Session,
        session_id: str,
        payload: UserReplyRequest,
        auth: AuthContext = LOCAL_AUTH,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        chat = self._get_chat(session, session_id, auth, for_update=True)
        execution = self._get_latest_execution_for_session(session, chat.id, for_update=True)
        if execution is None:
            raise ExecutionNotResumableError("No execution found for this session")
        resume_value = self._resume_value(payload)
        execution = self._resume_execution(session, execution.id, resume_value)
        return self._stream_execution(
            session_bind=session.get_bind(),
            execution_id=execution.id,
            tool_permissions=auth.tool_permissions,
            resume_value=resume_value,
        )

    def resume_and_run_background(
        self,
        session: Session,
        session_id: str,
        payload: UserReplyRequest,
        auth: AuthContext = LOCAL_AUTH,
    ) -> ChatSessionDetail:
        chat = self._get_chat(session, session_id, auth, for_update=True)
        execution = self._get_latest_execution_for_session(session, chat.id, for_update=True)
        if execution is None:
            raise ExecutionNotResumableError("No execution found for this session")
        resume_value = self._resume_value(payload)
        self._resume_execution(session, execution.id, resume_value)
        self._run_background(
            session_bind=session.get_bind(),
            execution_id=execution.id,
            tool_permissions=auth.tool_permissions,
            resume_value=resume_value,
        )
        return self.get_session(session, session_id, auth)

    def get_execution(
        self,
        session: Session,
        execution_id: str,
        auth: AuthContext = LOCAL_AUTH,
    ) -> ExecutionResponse:
        execution, invocation = self._get_execution_with_invocation(session, execution_id)
        chat = self._get_chat(session, invocation.session_id, auth)
        messages = self._get_messages_for_invocation(
            session,
            invocation_id=invocation.id,
        )
        payload = self._execution_response(execution=execution, chat_id=chat.id).model_dump()
        return ExecutionResponse(
            **payload,
            messages=[self._to_message_response(item) for item in messages],
            memory=self._conversation_memory(session, runtime=self.runtime, chat=chat),
        )

    def cancel_execution(
        self,
        session: Session,
        execution_id: str,
        auth: AuthContext = LOCAL_AUTH,
    ) -> ExecutionResponse:
        execution, invocation = self._get_execution_with_invocation(session, execution_id)
        self._get_chat(session, invocation.session_id, auth)
        execution = self._cancel_execution(session, execution.id)
        return self.get_execution(session, execution.id, auth)

    def resume_execution(
        self,
        session: Session,
        execution_id: str,
        resume_value: Any,
        auth: AuthContext = LOCAL_AUTH,
    ) -> ExecutionResponse:
        execution, invocation = self._get_execution_with_invocation(session, execution_id)
        self._get_chat(session, invocation.session_id, auth)
        execution = self._resume_execution(session, execution.id, resume_value)
        return self.get_execution(session, execution.id, auth)

    def _stream_execution(
        self,
        *,
        session_bind: Any,
        execution_id: str,
        tool_permissions: tuple[str, ...],
        resume_value: Any = None,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        sentinel = object()
        event_queue: queue.Queue[tuple[str, dict[str, Any]] | object] = queue.Queue(
            maxsize=MAX_STREAM_QUEUE_SIZE
        )
        thread = self._start_thread(
            session_bind=session_bind,
            execution_id=execution_id,
            tool_permissions=tool_permissions,
            resume_value=resume_value,
            event_queue=event_queue,
            sentinel=sentinel,
        )

        try:
            while True:
                event = event_queue.get()
                if event is sentinel:
                    break
                event_name, payload = event
                yield event_name, payload
        finally:
            self._release_thread(thread)

    def _run_background(
        self,
        *,
        session_bind: Any,
        execution_id: str,
        tool_permissions: tuple[str, ...],
        resume_value: Any = None,
    ) -> None:
        self._start_thread(
            session_bind=session_bind,
            execution_id=execution_id,
            tool_permissions=tool_permissions,
            resume_value=resume_value,
            event_queue=None,
            sentinel=None,
        )

    def _start_thread(
        self,
        *,
        session_bind: Any,
        execution_id: str,
        tool_permissions: tuple[str, ...],
        resume_value: Any,
        event_queue: queue.Queue[tuple[str, dict[str, Any]] | object] | None,
        sentinel: object | None,
    ) -> threading.Thread:
        with self._threads_lock:
            if self._closing:
                raise RuntimeError("Conversation service is shutting down")

        def execute_agent() -> None:
            writer: AgentEventWriter | None = None
            try:
                with Session(session_bind) as runtime_session:
                    if event_queue is not None:
                        writer = StreamingAgentEventWriter(execution_id, event_queue)
                    self.runner.run(
                        runtime_session,
                        execution_id=execution_id,
                        event_writer=writer,
                        tool_permissions=tool_permissions,
                        resume_value=resume_value,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Execution bootstrap failed: %s", execution_id)
                self._emit_fail_event(
                    event_queue=event_queue,
                    execution_id=execution_id,
                    error=str(exc),
                )
            finally:
                if writer is not None:
                    close_writer = getattr(writer, "close", None)
                    if callable(close_writer):
                        close_writer()
                if event_queue is not None and sentinel is not None:
                    self._emit_sentinel(event_queue, sentinel)
                self._release_thread(threading.current_thread())

        thread = threading.Thread(
            target=execute_agent,
            name=f"agent-execution-{execution_id}",
            daemon=True,
        )
        with self._threads_lock:
            self._threads.add(thread)
        thread.start()
        return thread

    def _release_thread(self, thread: threading.Thread) -> None:
        with self._threads_lock:
            self._threads.discard(thread)

    def _emit_fail_event(
        self,
        event_queue: queue.Queue[tuple[str, dict[str, Any]] | object] | None,
        execution_id: str,
        error: str,
    ) -> None:
        if event_queue is None:
            return
        try:
            event_queue.put(
                (
                    "token",
                    {
                        "name": "execution_failed",
                        "content": error or "execution_failed",
                        "execution_id": execution_id,
                    },
                ),
                timeout=0.5,
            )
        except queue.Full:
            logger.warning("Execution failed event dropped for %s", execution_id)

    def _emit_sentinel(
        self,
        event_queue: queue.Queue[tuple[str, dict[str, Any]] | object],
        sentinel: object,
    ) -> None:
        try:
            event_queue.put(sentinel, timeout=0.5)
        except queue.Full:
            try:
                event_queue.get_nowait()
                event_queue.put_nowait(sentinel)
            except queue.Full:
                return

    def _get_chat(self, session: Session, session_id: str, auth: AuthContext, *, for_update: bool = False) -> ChatSession:
        query = select(ChatSession).where(ChatSession.id == session_id)
        if for_update:
            query = query.with_for_update()
        chat = session.exec(query).first()
        if (
            chat is None
            or chat.tenant_id != auth.tenant_id
            or chat.owner_user_id != auth.user_id
            or not auth.can_access_account(chat.account_id)
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

    def _get_executions_for_session(self, session: Session, session_id: str) -> list[AgentExecution]:
        query = (
            select(AgentExecution)
            .join(AgentInvocation, AgentExecution.invocation_id == AgentInvocation.id)
            .where(AgentInvocation.session_id == session_id)
            .order_by(AgentExecution.created_at.asc())
        )
        return list(session.exec(query).all())

    def _get_messages_for_invocation(self, session: Session, *, invocation_id: str) -> list[ChatMessage]:
        return list(
            session.exec(
                select(ChatMessage)
                .where(ChatMessage.invocation_id == invocation_id)
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
            .where(AgentExecution.status.in_(ACTIVE_EXECUTION_STATUSES))
        )
        if for_update:
            query = query.with_for_update()
        if session.exec(query).first() is not None:
            raise ActiveExecutionExistsError("Chat session already has an active execution")

    def _create_user_message_invocation_execution(
        self,
        session: Session,
        *,
        chat: ChatSession,
        content: str,
        auth: AuthContext,
    ) -> tuple[ChatMessage, AgentInvocation, AgentExecution]:
        invocation = AgentInvocation(
            session_id=chat.id,
            account_id=chat.account_id,
            tenant_id=auth.tenant_id,
            created_by_user_id=auth.user_id,
        )
        session.add(invocation)
        session.flush()

        message = ChatMessage(
            session_id=chat.id,
            invocation_id=invocation.id,
            role=MessageRole.user,
            message_type=MessageType.text,
            message_metadata={"invocation_id": invocation.id},
            content=content,
        )
        session.add(message)
        session.flush()

        invocation.user_message_id = message.id
        session.add(invocation)

        execution = AgentExecution(invocation_id=invocation.id)
        session.add(execution)

        chat.touch_updated_at()
        session.add(chat)
        return message, invocation, execution

    def _get_execution_with_invocation(
        self,
        session: Session,
        execution_id: str,
    ) -> tuple[AgentExecution, AgentInvocation]:
        execution = session.get(AgentExecution, execution_id)
        if execution is None:
            raise ExecutionNotFoundError(execution_id)
        invocation = session.get(AgentInvocation, execution.invocation_id)
        if invocation is None:
            raise ExecutionNotFoundError(execution_id)
        return execution, invocation

    def _cancel_execution(self, session: Session, execution_id: str) -> AgentExecution:
        execution = session.get(AgentExecution, execution_id)
        if execution is None:
            raise ExecutionNotFoundError(execution_id)
        if execution.status in TERMINAL_EXECUTION_STATUSES:
            return execution
        from models.base import utcnow
        now = utcnow()
        execution.cancel_requested_at = execution.cancel_requested_at or now
        execution.touch_updated_at(now)
        session.add(execution)
        session.commit()
        session.refresh(execution)
        return execution

    def _resume_execution(self, session: Session, execution_id: str, resume_value: Any) -> AgentExecution:
        execution = session.get(AgentExecution, execution_id)
        if execution is None:
            raise ExecutionNotFoundError(execution_id)
        if execution.status != RunStatus.interrupted:
            raise ExecutionNotResumableError("Only interrupted executions can be resumed")
        if resume_value is None:
            raise ExecutionResumeValueRequiredError("Resume value is required")
        execution.status = RunStatus.running
        execution.error = ""
        execution.finished_at = None
        execution.cancel_requested_at = None
        execution.touch_updated_at()
        session.add(execution)
        session.commit()
        session.refresh(execution)
        return execution

    @staticmethod
    def _resume_value(payload: UserReplyRequest) -> Any:
        value = getattr(payload, "message", None)
        if value is None:
            raise ExecutionResumeValueRequiredError("Resume value is required")
        return value

    def _get_session_messages(
        self,
        session: Session,
        *,
        session_id: str,
        request: MessageListRequest,
    ) -> tuple[list[ChatMessage], str | None]:
        requested_cursor = request.before or request.cursor
        cursor_filter = self._decode_cursor(requested_cursor)

        statement = select(ChatMessage).where(ChatMessage.session_id == session_id)
        if cursor_filter is not None:
            cursor_time, cursor_message_id = cursor_filter
            statement = statement.where(
                or_(
                    ChatMessage.created_at < cursor_time,
                    and_(
                        ChatMessage.created_at == cursor_time,
                        ChatMessage.id < cursor_message_id,
                    ),
                )
            )

        statement = statement.order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        rows = list(session.exec(statement.limit(request.limit + 1)).all())
        if not rows:
            return [], None

        has_more = len(rows) > request.limit
        rows = rows[: request.limit]
        rows.sort(key=lambda item: (item.created_at, item.id))

        next_cursor = None
        if has_more:
            oldest = rows[0]
            next_cursor = self._encode_cursor(oldest.created_at, oldest.id)
        return rows, next_cursor

    @staticmethod
    def _encode_cursor(created_at: datetime, message_id: str) -> str:
        return f"{created_at.isoformat()}{_MESSAGE_CURSOR_SEP}{message_id}"

    @staticmethod
    def _decode_cursor(cursor: str | None) -> tuple[datetime, str] | None:
        if cursor is None:
            return None
        if not isinstance(cursor, str):
            raise InvalidCursorError("Cursor must be a string")
        if not cursor.strip():
            raise InvalidCursorError("Cursor cannot be empty")
        if _MESSAGE_CURSOR_SEP not in cursor:
            raise InvalidCursorError("Cursor format is invalid")
        created_at_raw, message_id = cursor.split(_MESSAGE_CURSOR_SEP, 1)
        if not created_at_raw or not message_id:
            raise InvalidCursorError("Cursor format is invalid")
        try:
            parsed = datetime.fromisoformat(created_at_raw)
        except ValueError as exc:
            raise InvalidCursorError("Cursor timestamp is invalid") from exc
        return parsed, message_id

    @staticmethod
    def _session_summary(
        chat: ChatSession,
        *,
        latest_status: RunStatus | None,
        message_count: int = 0,
    ) -> ChatSessionSummary:
        return ChatSessionSummary(
            session_id=chat.id,
            account_id=chat.account_id,
            title=chat.title,
            created_at=chat.created_at,
            updated_at=chat.updated_at,
            latest_status=latest_status,
            status=latest_status,
            message_count=message_count,
        )

    @staticmethod
    def _execution_response(*, execution: AgentExecution, chat_id: str) -> ChatExecutionResponse:
        return ChatExecutionResponse(
            id=execution.id,
            session_id=chat_id,
            status=execution.status,
            trace_id=execution.id,
            error=execution.error,
            started_at=execution.started_at,
            finished_at=execution.finished_at,
            cancel_requested_at=execution.cancel_requested_at,
            interrupt_payload={},
        )

    @staticmethod
    def _conversation_memory(
        session: Session,
        runtime: object,
        chat: ChatSession,
    ) -> ConversationMemory:
        repository = MemoryRepository(session)
        short_term = ShortTermMemory(repository)
        long_term = LongTermMemory(repository, runtime.store)
        return ConversationMemory(
            short_term_summary=short_term.load_summary(
                chat.id,
                tenant_id=chat.tenant_id,
                user_id=chat.owner_user_id,
                account_id=chat.account_id,
            ),
            long_term_memories=[
                item
                for item in long_term.list_all(
                    chat.account_id,
                    tenant_id=chat.tenant_id,
                    user_id=chat.owner_user_id,
                    limit=20,
                )
            ],
        )

    @staticmethod
    def _to_message_response(item: ChatMessage) -> ChatMessageResponse:
        metadata = item.message_metadata if isinstance(item.message_metadata, dict) else {}
        trace_id = str(
            metadata.get("execution_id")
            or metadata.get("trace_id")
            or metadata.get("stream_id")
            or ""
        )
        return ChatMessageResponse(
            id=item.id,
            role=item.role,
            message_type=item.message_type,
            content=item.content,
            status=MessageState.completed,
            tool_name=item.tool_name,
            tool_call_id=item.tool_call_id,
            model_name=item.model_name,
            input_tokens=item.input_tokens,
            output_tokens=item.output_tokens,
            latency_ms=item.latency_ms,
            trace_id=trace_id,
            citations=ConversationService._message_citations(metadata.get("citations")),
            tool_results=ConversationService._message_tool_results(metadata.get("tool_results")),
            created_at=item.created_at,
        )

    @staticmethod
    def _message_tool_results(value: Any) -> list[ToolExecutionResult]:
        if not isinstance(value, list):
            return []
        parsed: list[ToolExecutionResult] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            try:
                parsed.append(
                    ToolExecutionResult(
                        tool_name=str(item.get("tool_name", "")),
                        result=item.get("result"),
                        error=ConversationService._to_error(item.get("error")),
                    )
                )
            except Exception:
                continue
        return parsed

    @staticmethod
    def _message_citations(value: Any) -> list[MessageCitation]:
        if not isinstance(value, list):
            return []
        parsed: list[MessageCitation] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            source = item.get("source")
            if not isinstance(source, str) or not source.strip():
                continue
            url = item.get("url")
            try:
                parsed.append(
                    MessageCitation(
                        source=source,
                        url=url if isinstance(url, str) or url is None else str(url),
                    )
                )
            except Exception:
                continue
        return parsed

    @staticmethod
    def _to_error(value: Any) -> ErrorDetail | None:
        if value is None:
            return None
        if isinstance(value, dict):
            code = value.get("code", "TOOL_ERROR")
            message = value.get("message", "")
            msg = str(message).strip()
            if not msg:
                return None
            return ErrorDetail(
                code=str(code) or "TOOL_ERROR",
                message=msg,
                retryable=bool(value.get("retryable", False)),
            )
        msg = str(value).strip()
        if not msg:
            return None
        return ErrorDetail(code="TOOL_ERROR", message=msg, retryable=False)


__all__ = ["ConversationService"]
