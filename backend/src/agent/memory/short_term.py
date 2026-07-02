from __future__ import annotations

from agent.memory.repository import MemoryRepository
from agent.memory.summary import summarize_messages
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from models.chat import ChatMessage
from models.enums import MessageRole, MessageType
from sqlmodel import Session, select

SHORT_TERM_NAMESPACE_LABEL = "short_term"
SHORT_TERM_SUMMARY_KEY = "summary"
MAX_WINDOW_MESSAGES = 40


def short_term_namespace(session_id: str) -> tuple[str, ...]:
    return ("sessions", session_id, SHORT_TERM_NAMESPACE_LABEL)


class ShortTermMemory:
    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    def load(self, session: Session, *, session_id: str) -> tuple[str, list[BaseMessage]]:
        messages = self.load_messages(session, session_id=session_id)
        summary = self.load_summary(session_id)
        if not summary:
            summary = summarize_messages(messages)
            if summary:
                self.save_summary(session_id, summary)
        return summary, messages

    def load_messages(self, session: Session, *, session_id: str) -> list[BaseMessage]:
        rows = session.exec(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at.asc())
        ).all()
        messages = [message for row in rows if (message := self._to_langchain_message(row))]
        return messages[-MAX_WINDOW_MESSAGES:]

    def load_summary(self, session_id: str) -> str:
        entry = self.repository.get(short_term_namespace(session_id), SHORT_TERM_SUMMARY_KEY)
        return entry.content if entry else ""

    def save_summary(self, session_id: str, summary: str) -> None:
        if not summary.strip():
            return
        self.repository.upsert(
            short_term_namespace(session_id),
            SHORT_TERM_SUMMARY_KEY,
            content=summary.strip(),
            kind="summary",
            payload={"scope": "thread"},
        )

    def refresh_summary(self, session: Session, *, session_id: str) -> str:
        messages = self.load_messages(session, session_id=session_id)
        summary = summarize_messages(messages)
        self.save_summary(session_id, summary)
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
        if row.role == MessageRole.system:
            return SystemMessage(content=content, id=row.id)
        return None
