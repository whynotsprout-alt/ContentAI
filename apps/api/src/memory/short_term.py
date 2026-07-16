from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from memory.repository import MemoryRepository
from memory.summary import merge_summaries, summarize_messages
from models.chat import ChatMessage
from models.enums import (
    MemoryKind,
    MemorySourceType,
    MessageRole,
    MessageType,
)
from sqlalchemy import and_, or_
from sqlmodel import Session, select

SHORT_TERM_SUMMARY_KEY = "summary"
MAX_WINDOW_MESSAGES = 40


class ShortTermMemory:
    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    def load(
        self,
        session: Session,
        *,
        session_id: str,
        user_id: str,
    ) -> tuple[str, list[BaseMessage]]:
        messages = self.load_messages(session, session_id=session_id)
        summary = self.load_summary(
            session_id,
            user_id=user_id,
        )
        if not summary:
            summary = self.refresh_summary(
                session,
                session_id=session_id,
                user_id=user_id,
            )
        return summary, messages

    def load_messages(self, session: Session, *, session_id: str) -> list[BaseMessage]:
        rows = session.exec(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at.asc())
        ).all()
        messages = [message for row in rows if (message := self._to_langchain_message(row))]
        return messages[-MAX_WINDOW_MESSAGES:]

    def load_summary(
        self,
        session_id: str,
        *,
        user_id: str,
    ) -> str:
        entry = self.repository.get(
            SHORT_TERM_SUMMARY_KEY,
            user_id=user_id,
            session_id=session_id,
        )
        return entry.content if entry else ""

    def save_summary(
        self,
        session_id: str,
        summary: str,
        *,
        user_id: str,
        cursor_created_at: str | None = None,
        cursor_message_id: str | None = None,
    ) -> None:
        if not summary.strip():
            return
        self.repository.upsert(
            SHORT_TERM_SUMMARY_KEY,
            content=summary.strip(),
            kind=MemoryKind.summary,
            payload={
                "scope": "thread",
                "cursor_created_at": cursor_created_at,
                "cursor_message_id": cursor_message_id,
            },
            user_id=user_id,
            session_id=session_id,
            source_type=MemorySourceType.summary,
            source_session_id=session_id,
        )

    def refresh_summary(
        self,
        session: Session,
        *,
        session_id: str,
        user_id: str,
    ) -> str:
        entry = self.repository.get(
            SHORT_TERM_SUMMARY_KEY,
            user_id=user_id,
            session_id=session_id,
        )
        previous = entry.content if entry else ""
        payload = entry.payload if entry and isinstance(entry.payload, dict) else {}
        cursor_created_at = payload.get("cursor_created_at")
        cursor_message_id = payload.get("cursor_message_id")
        statement = (
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .where(ChatMessage.role.in_([MessageRole.user, MessageRole.assistant]))
            .where(ChatMessage.message_type.in_([MessageType.text, MessageType.markdown]))
            .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        )
        if isinstance(cursor_created_at, str) and isinstance(cursor_message_id, str):
            from datetime import datetime

            try:
                cursor_time = datetime.fromisoformat(cursor_created_at)
            except ValueError:
                cursor_time = None
            if cursor_time is not None:
                statement = statement.where(
                    or_(
                        ChatMessage.created_at > cursor_time,
                        and_(
                            ChatMessage.created_at == cursor_time,
                            ChatMessage.id > cursor_message_id,
                        ),
                    )
                )
        rows = list(session.exec(statement).all())
        summary = previous
        for offset in range(0, len(rows), 12):
            messages = [
                message
                for row in rows[offset : offset + 12]
                if (message := self._to_langchain_message(row)) is not None
            ]
            summary = merge_summaries(summary, summarize_messages(messages, max_items=12))
        latest = rows[-1] if rows else None
        self.save_summary(
            session_id,
            summary,
            user_id=user_id,
            cursor_created_at=(
                latest.created_at.isoformat()
                if latest is not None
                else cursor_created_at
                if isinstance(cursor_created_at, str)
                else None
            ),
            cursor_message_id=(
                latest.id
                if latest is not None
                else cursor_message_id
                if isinstance(cursor_message_id, str)
                else None
            ),
        )
        return summary

    @staticmethod
    def _to_langchain_message(row: ChatMessage) -> BaseMessage | None:
        if row.message_type not in {MessageType.text, MessageType.markdown}:
            return None
        content = row.content.strip()
        if not content:
            return None
        if row.role == MessageRole.user:
            return HumanMessage(content=content, id=row.id)
        if row.role == MessageRole.assistant:
            return AIMessage(content=content, id=row.id)
        return None
