from __future__ import annotations

from typing import Any

from agent.runtime.schemas import validate_assistant_response
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from models.chat import ChatMessage
from models.enums import MessageRole, MessageType
from sqlmodel import Session, select


class MessagePersister:
    def persist_graph_messages(
        self,
        db_session: Session,
        *,
        session_id: str,
        invocation_id: str,
        execution_id: str,
        messages: list[BaseMessage],
        event_writer: Any,
        streamed_assistant_text: str = "",
    ) -> ChatMessage | None:
        for message in reversed(messages):
            if isinstance(message, HumanMessage) or not isinstance(message, AIMessage):
                continue
            content = message_to_text(message)
            if not content or message.tool_calls:
                continue
            return self.persist_assistant_text(
                db_session,
                session_id=session_id,
                invocation_id=invocation_id,
                execution_id=execution_id,
                content=content,
                event_writer=event_writer,
                emit_delta=not streamed_assistant_text,
            )
        return None

    def persist_assistant_text(
        self,
        db_session: Session,
        *,
        session_id: str,
        invocation_id: str,
        execution_id: str,
        content: str,
        event_writer: Any,
        emit_delta: bool = True,
        commit: bool = False,
        pending_events: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> ChatMessage:
        existing = db_session.exec(
            select(ChatMessage).where(
                ChatMessage.execution_id == execution_id,
                ChatMessage.role == MessageRole.assistant,
            )
        ).one_or_none()
        if existing is not None:
            return existing
        response = validate_assistant_response(
            content=content,
            message_type="markdown",
            metadata={},
        )
        message = ChatMessage(
            session_id=session_id,
            invocation_id=invocation_id,
            execution_id=execution_id,
            role=MessageRole.assistant,
            message_type=MessageType.markdown,
            content=response.content,
        )
        db_session.add(message)
        db_session.flush()
        if commit:
            db_session.commit()
            db_session.refresh(message)
        return message


def message_to_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts).strip()
    return str(content).strip()


__all__ = [
    "MessagePersister",
    "message_to_text",
]
