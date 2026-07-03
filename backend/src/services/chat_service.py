from __future__ import annotations

from agent.memory import LongTermMemory, MemoryRepository, ShortTermMemory
from agent.runtime.executor import agent_executor
from models.account import Account
from models.base import json_dumps, json_loads, utcnow
from models.chat import AgentRun, AgentRunEvent, ChatMessage, ChatSession
from models.enums import MessageRole, MessageType, RunStatus
from models.schemas import (
    ChatMessageResponse,
    ChatSessionDetail,
    ChatSessionSummary,
    ChatUserMessageResponse,
    ConversationMemory,
    CreateSessionResponse,
    MemoryItem,
    RunCreateRequest,
    RunResponse,
)
from services.errors import AccountNotFoundError
from sqlalchemy import and_, func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

BUSY_RUN_STATUSES = {RunStatus.queued, RunStatus.running}
TERMINAL_RUN_STATUSES = {
    RunStatus.completed,
    RunStatus.failed,
    RunStatus.cancelled,
    RunStatus.interrupted,
}


class ChatSessionNotFoundError(Exception):
    pass


class ActiveRunExistsError(Exception):
    pass


class RunNotFoundError(Exception):
    pass


class ChatService:
    def create_session(self, session: Session) -> CreateSessionResponse:
        chat = ChatSession()
        session.add(chat)
        session.commit()
        session.refresh(chat)
        return CreateSessionResponse(session_id=chat.id, title=chat.title)

    def list_sessions(self, session: Session) -> list[ChatSessionSummary]:
        latest_runs = (
            select(
                AgentRun.session_id.label("session_id"),
                AgentRun.id.label("run_id"),
                AgentRun.status.label("status"),
                func.row_number()
                .over(
                    partition_by=AgentRun.session_id,
                    order_by=AgentRun.updated_at.desc(),
                )
                .label("rank"),
            )
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
                latest_runs.c.run_id,
                latest_runs.c.status,
                func.coalesce(message_counts.c.message_count, 0),
            )
            .outerjoin(
                latest_runs,
                and_(latest_runs.c.session_id == ChatSession.id, latest_runs.c.rank == 1),
            )
            .outerjoin(message_counts, message_counts.c.session_id == ChatSession.id)
            .order_by(ChatSession.updated_at.desc())
        ).all()
        return [
            ChatSessionSummary(
                session_id=chat.id,
                title=chat.title,
                created_at=chat.created_at,
                updated_at=chat.updated_at,
                latest_run_id=latest_run_id,
                latest_status=latest_status,
                message_count=int(message_count or 0),
            )
            for chat, latest_run_id, latest_status, message_count in rows
        ]

    def get_session(self, session: Session, session_id: str) -> ChatSessionDetail:
        chat = self._get_chat(session, session_id)
        latest_run = self._latest_run_for_session(session, session_id=session_id)
        account_id = latest_run.account_id if latest_run else None
        memory = (
            self._conversation_memory(session, session_id=session_id, account_id=account_id)
            if account_id
            else ConversationMemory()
        )
        messages = self._session_messages(session, session_id)
        return ChatSessionDetail(
            **self._to_session_summary(chat, session).model_dump(),
            memory=memory,
            messages=[self._message_response(item) for item in messages],
        )

    def create_run(self, session: Session, payload: RunCreateRequest) -> ChatUserMessageResponse:
        if session.get(Account, payload.account_id) is None:
            raise AccountNotFoundError(payload.account_id)

        if payload.session_id:
            chat = self._get_chat_for_account(
                session,
                session_id=payload.session_id,
                account_id=payload.account_id,
            )
        else:
            chat = ChatSession()
            session.add(chat)
            session.flush()

        message, run = self._create_user_message_and_run(
            session,
            chat=chat,
            account_id=payload.account_id,
            content=payload.message,
        )
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise ActiveRunExistsError("Chat session already has an active run") from exc
        session.refresh(message)
        session.refresh(run)

        memory = self._conversation_memory(
            session,
            session_id=chat.id,
            account_id=payload.account_id,
        )
        return ChatUserMessageResponse(
            session_id=chat.id,
            message_id=message.id,
            run_id=run.id,
            status=run.status,
            memory=memory,
            error=run.error,
        )

    def get_run(self, session: Session, run_id: str) -> RunResponse:
        run = self._get_run(session, run_id)
        messages = self._session_messages(session, run.session_id)
        return RunResponse(
            id=run.id,
            session_id=run.session_id,
            account_id=run.account_id,
            user_message_id=run.user_message_id,
            user_message=run.user_message,
            status=run.status,
            error=run.error,
            attempt_count=run.attempt_count,
            started_at=run.started_at,
            finished_at=run.finished_at,
            cancel_requested_at=run.cancel_requested_at,
            messages=[self._message_response(item) for item in messages],
            memory=self._conversation_memory(
                session,
                session_id=run.session_id,
                account_id=run.account_id,
            ),
        )

    def cancel_run(self, session: Session, run_id: str) -> RunResponse:
        run = self._get_run(session, run_id)
        if run.status in TERMINAL_RUN_STATUSES:
            return self.get_run(session, run_id)

        now = utcnow()
        run.cancel_requested_at = run.cancel_requested_at or now
        if run.status == RunStatus.queued:
            run.status = RunStatus.cancelled
            run.finished_at = now
            run.lease_owner = None
            run.lease_expires_at = None
        run.touch_updated_at(now)
        session.add(run)
        session.commit()
        session.refresh(run)
        return self.get_run(session, run_id)

    def event_rows_for_run(
        self,
        session: Session,
        *,
        run_id: str,
        after_event_id: int,
    ) -> list[AgentRunEvent]:
        self._get_run(session, run_id)
        return list(
            session.exec(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id == run_id)
                .where(AgentRunEvent.id > after_event_id)
                .order_by(AgentRunEvent.id)
            ).all()
        )

    def event_rows_for_session(
        self,
        session: Session,
        *,
        session_id: str,
        account_id: str,
        after_event_id: int,
    ) -> list[AgentRunEvent]:
        self._get_chat_for_account(session, session_id=session_id, account_id=account_id)
        run_ids = list(
            session.exec(
                select(AgentRun.id)
                .where(AgentRun.session_id == session_id)
                .where(AgentRun.account_id == account_id)
            ).all()
        )
        if not run_ids:
            return []
        return list(
            session.exec(
                select(AgentRunEvent)
                .where(AgentRunEvent.run_id.in_(run_ids))
                .where(AgentRunEvent.id > after_event_id)
                .order_by(AgentRunEvent.id)
            ).all()
        )

    def is_terminal_run(self, session: Session, run_id: str) -> bool:
        run = self._get_run(session, run_id)
        return run.status in TERMINAL_RUN_STATUSES

    def run_status(self, session: Session, run_id: str) -> RunStatus:
        return self._get_run(session, run_id).status

    def _create_user_message_and_run(
        self,
        session: Session,
        *,
        chat: ChatSession,
        account_id: str,
        content: str,
    ) -> tuple[ChatMessage, AgentRun]:
        latest_run = self._latest_run_for_session(
            session,
            session_id=chat.id,
            account_id=account_id,
        )
        if latest_run is not None and latest_run.status in BUSY_RUN_STATUSES:
            raise ActiveRunExistsError("Chat session already has an active run")

        run = AgentRun(session_id=chat.id, account_id=account_id, user_message=content)
        session.add(run)
        session.flush()

        message = ChatMessage(
            session_id=chat.id,
            role=MessageRole.user,
            message_type=MessageType.text,
            message_metadata=json_dumps({"run_id": run.id}),
            content=content,
            run_id=run.id,
        )
        session.add(message)
        session.flush()

        run.user_message_id = message.id
        session.add(run)
        if chat.title == "New Session":
            chat.title = content[:32] or chat.title
        chat.touch_updated_at()
        session.add(chat)
        return message, run

    @staticmethod
    def _conversation_memory(
        session: Session,
        *,
        session_id: str,
        account_id: str,
    ) -> ConversationMemory:
        repository = MemoryRepository(session)
        short_term = ShortTermMemory(repository)
        long_term = LongTermMemory(repository, agent_executor.store)
        return ConversationMemory(
            short_term_summary=short_term.load_summary(session_id),
            long_term_memories=[
                MemoryItem(
                    key=item.key,
                    kind=item.kind,
                    content=item.content,
                    payload=item.payload,
                    updated_at=item.updated_at,
                )
                for item in long_term.list_all(account_id, limit=20)
            ],
        )

    @staticmethod
    def _session_messages(session: Session, session_id: str) -> list[ChatMessage]:
        return list(
            session.exec(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.created_at.asc())
            ).all()
        )

    def _to_session_summary(self, chat: ChatSession, session: Session) -> ChatSessionSummary:
        latest_run = self._latest_run_for_session(session, session_id=chat.id)
        return ChatSessionSummary(
            session_id=chat.id,
            title=chat.title,
            created_at=chat.created_at,
            updated_at=chat.updated_at,
            latest_run_id=latest_run.id if latest_run else None,
            latest_status=latest_run.status if latest_run else None,
            message_count=len(
                session.exec(select(ChatMessage.id).where(ChatMessage.session_id == chat.id)).all()
            ),
        )

    @staticmethod
    def _message_response(item: ChatMessage) -> ChatMessageResponse:
        return ChatMessageResponse(
            id=item.id,
            role=item.role,
            message_type=item.message_type,
            content=item.content,
            run_id=item.run_id,
            tool_name=item.tool_name,
            tool_call_id=item.tool_call_id,
            parent_message_id=item.parent_message_id,
            created_at=item.created_at,
        )

    @staticmethod
    def event_payload(row: AgentRunEvent) -> dict[str, object] | None:
        if row.id is None or not row.event.strip() or "\n" in row.event or "\r" in row.event:
            return None
        data = json_loads(row.payload, default={})
        if not isinstance(data, dict):
            return None
        data.setdefault("created_at", row.created_at.isoformat())
        return {"id": row.id, "event": row.event.strip(), "data": data}

    @staticmethod
    def _get_chat(session: Session, session_id: str) -> ChatSession:
        chat = session.get(ChatSession, session_id)
        if chat is None:
            raise ChatSessionNotFoundError(session_id)
        return chat

    def _get_chat_for_account(
        self,
        session: Session,
        *,
        session_id: str,
        account_id: str,
    ) -> ChatSession:
        chat = self._get_chat(session, session_id)
        latest_run = self._latest_run_for_session(session, session_id=session_id)
        if latest_run is not None and latest_run.account_id != account_id:
            raise ChatSessionNotFoundError(session_id)
        return chat

    @staticmethod
    def _get_run(session: Session, run_id: str) -> AgentRun:
        run = session.get(AgentRun, run_id)
        if run is None:
            raise RunNotFoundError(run_id)
        return run

    @staticmethod
    def _latest_run_for_session(
        session: Session,
        *,
        session_id: str,
        account_id: str | None = None,
    ) -> AgentRun | None:
        query = select(AgentRun).where(AgentRun.session_id == session_id)
        if account_id is not None:
            query = query.where(AgentRun.account_id == account_id)
        return session.exec(query.order_by(AgentRun.updated_at.desc())).first()


chat_service = ChatService()
