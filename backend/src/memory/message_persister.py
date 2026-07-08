from __future__ import annotations

import json
from typing import Any

from agent.runtime.events import now_utc
from agent.runtime.schemas import validate_assistant_response
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from models.chat import ChatMessage, ToolExecution
from models.enums import MessageRole, MessageType, ToolExecutionStatus
from sqlmodel import Session


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
    ) -> bool:
        assistant_saved = False
        pending_events: list[tuple[str, dict[str, Any]]] = []
        for message in messages:
            if isinstance(message, HumanMessage):
                continue
            if isinstance(message, ToolMessage):
                self.persist_tool_message(
                    db_session,
                    session_id=session_id,
                    invocation_id=invocation_id,
                    execution_id=execution_id,
                    message=message,
                    event_writer=event_writer,
                    commit=False,
                    pending_events=pending_events,
                )
                continue
            if isinstance(message, AIMessage):
                content = message_to_text(message)
                if not content or message.tool_calls:
                    continue
                self.persist_assistant_text(
                    db_session,
                    session_id=session_id,
                    invocation_id=invocation_id,
                    execution_id=execution_id,
                    content=content,
                    metadata={"execution_id": execution_id, "invocation_id": invocation_id},
                    event_writer=event_writer,
                    emit_delta=not streamed_assistant_text,
                    commit=False,
                    pending_events=pending_events,
                )
                assistant_saved = True
        if pending_events:
            db_session.commit()
            for event, payload in pending_events:
                event_writer.emit(event, payload)
        return assistant_saved

    def persist_tool_message(
        self,
        db_session: Session,
        *,
        session_id: str,
        invocation_id: str,
        execution_id: str,
        message: ToolMessage,
        event_writer: Any,
        commit: bool = True,
        pending_events: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> None:
        content = message_to_text(message)
        db_session.add(
            ChatMessage(
                session_id=session_id,
                invocation_id=invocation_id,
                role=MessageRole.tool,
                message_type=MessageType.json if looks_like_json(content) else MessageType.text,
                message_metadata={"execution_id": execution_id, "invocation_id": invocation_id},
                content=content[:240000],
                tool_name=message.name,
                tool_call_id=message.tool_call_id,
            )
        )
        now = now_utc()
        db_session.add(
            ToolExecution(
                execution_id=execution_id,
                tool_name=message.name or "tool",
                tool_call_id=message.tool_call_id,
                arguments={},
                result=_json_value(content),
                status=ToolExecutionStatus.completed,
                started_at=now,
                finished_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        payload = {
            "execution_id": execution_id,
            "tool_name": message.name,
            "tool_call_id": message.tool_call_id,
        }
        if commit:
            db_session.commit()
            event_writer.emit("tool_call_completed", payload)
        elif pending_events is not None:
            pending_events.append(("tool_call_completed", payload))

    def persist_assistant_text(
        self,
        db_session: Session,
        *,
        session_id: str,
        invocation_id: str,
        execution_id: str,
        content: str,
        metadata: dict[str, Any],
        event_writer: Any,
        emit_delta: bool = True,
        commit: bool = True,
        pending_events: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> None:
        response = validate_assistant_response(
            content=content,
            message_type="markdown",
            metadata={
                key: value
                for key, value in metadata.items()
                if isinstance(value, str | int | float | bool) or value is None
            },
        )
        message_metadata = metadata.copy()
        message_metadata["structured_response"] = response.model_dump()
        db_session.add(
            ChatMessage(
                session_id=session_id,
                invocation_id=invocation_id,
                role=MessageRole.assistant,
                message_type=MessageType.markdown,
                message_metadata=message_metadata,
                content=response.content,
            )
        )
        events = [
            (
                "assistant_message_delta",
                {
                    "execution_id": execution_id,
                    "message_type": response.message_type,
                    "chunk": response.content if emit_delta else "",
                    "done": True,
                },
            ),
            (
                "assistant_message",
                {
                    "execution_id": execution_id,
                    "message_type": response.message_type,
                    "content": response.content,
                },
            ),
        ]
        if commit:
            db_session.commit()
            for event, payload in events:
                event_writer.emit(event, payload)
        elif pending_events is not None:
            pending_events.extend(events)


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


def looks_like_json(value: str) -> bool:
    try:
        json.loads(value)
    except json.JSONDecodeError:
        return False
    return True


def _json_value(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {"content": value[:240000]}


__all__ = [
    "MessagePersister",
    "message_to_text",
]
