from __future__ import annotations

import html
import json
import re
from datetime import timedelta
from typing import Any

from agent.context.assembler import ContextAssembler
from agent.graph.factory import build_agent_graph
from agent.infrastructure.llm import model_gateway
from agent.memory import LongTermMemory, MemoryEntry, MemoryRepository, ShortTermMemory
from agent.runtime.checkpoint import build_checkpointer, build_store, checkpoint_messages
from agent.runtime.context import ToolRuntimeContext, tool_runtime_scope
from agent.runtime.events import AgentEventWriter, event_writer_scope, now_utc
from agent.runtime.schemas import validate_assistant_response
from agent.tools.registry import build_tool_set, tool_names
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from models.account import Account
from models.base import json_dumps
from models.chat import AgentRun, ChatMessage
from models.enums import MessageRole, MessageType, RunStatus
from sqlmodel import Session

MAX_AGENT_ITERATIONS = 8
HEARTBEAT_INTERVAL_SECONDS = 15
TERMINAL_RUN_STATUSES = {
    RunStatus.completed,
    RunStatus.failed,
    RunStatus.cancelled,
    RunStatus.interrupted,
}


class RunCancelled(Exception):
    pass


class AgentExecutor:
    def __init__(self) -> None:
        self.tools = build_tool_set()
        self.checkpointer = build_checkpointer()
        self.store = build_store()
        self.model = model_gateway.build_agent_model(tools=self.tools)
        self.graph = build_agent_graph(
            model=self.model,
            tools=self.tools,
            checkpointer=self.checkpointer,
            store=self.store,
        )
        self.assembler = ContextAssembler()

    def run(self, db_session: Session, *, run_id: str) -> None:
        run = db_session.get(AgentRun, run_id)
        if run is None or run.status in TERMINAL_RUN_STATUSES:
            return

        event_writer = AgentEventWriter(run.id, db_session)
        try:
            self._ensure_run_not_cancelled(db_session, run.id)
            self._set_run_state(db_session, run, RunStatus.running, "")
            event_writer.emit("run_started", {"run_id": run.id})

            account = db_session.get(Account, run.account_id)
            if account is None:
                raise RuntimeError("Account not found for agent run.")

            repository = MemoryRepository(db_session)
            short_term = ShortTermMemory(repository)
            long_term = LongTermMemory(repository, self.store)
            long_term.remember_from_user_message(run.account_id, run.user_message)

            short_summary, db_messages = short_term.load(db_session, session_id=run.session_id)
            recalled = long_term.recall(run.account_id, run.user_message, limit=8)
            agent_context = self.assembler.assemble(
                account=account,
                messages=db_messages,
                short_term_summary=short_summary,
                long_term_memories=recalled,
                tool_names=tool_names(self.tools),
            )

            thread_id = run.session_id
            checkpoint_history = checkpoint_messages(self.checkpointer, thread_id=thread_id)
            graph_input_messages = (
                [HumanMessage(content=run.user_message)]
                if checkpoint_history
                else agent_context.messages
            )
            before_count = (
                len(checkpoint_history)
                if checkpoint_history
                else len(graph_input_messages)
            )

            state = {
                "messages": graph_input_messages,
                "system_prompt": agent_context.system_prompt,
                "iterations": 0,
                "max_iterations": MAX_AGENT_ITERATIONS,
            }
            config = {
                "configurable": {"thread_id": thread_id},
                "recursion_limit": MAX_AGENT_ITERATIONS * 2 + 4,
            }
            tool_context = ToolRuntimeContext(
                run_id=run.id,
                session_id=run.session_id,
                account_id=run.account_id,
                allowed_hotspot_sources=account.hotspot_sources,
                long_term_memory=long_term,
            )
            with event_writer_scope(event_writer), tool_runtime_scope(tool_context):
                new_messages, streamed_assistant_text = self._stream_graph(
                    db_session=db_session,
                    run=run,
                    state=state,
                    config=config,
                    event_writer=event_writer,
                    run_id=run.id,
                )

            if not new_messages:
                result = self.graph.invoke(state, config=config)
                result_messages = result.get("messages", []) if isinstance(result, dict) else []
                new_messages = result_messages[before_count:]
                streamed_assistant_text = ""

            persisted_assistant = self._persist_graph_messages(
                db_session,
                run=run,
                messages=new_messages,
                event_writer=event_writer,
                streamed_assistant_text=streamed_assistant_text,
            )
            if not persisted_assistant:
                self._persist_assistant_text(
                    db_session,
                    run=run,
                    content=(
                        "The request completed, but the model did not return "
                        "displayable content."
                    ),
                    metadata={"run_id": run.id, "fallback": True},
                    event_writer=event_writer,
                    emit_delta=not streamed_assistant_text,
                )

            short_term.refresh_summary(db_session, session_id=run.session_id)
            self._set_run_state(db_session, run, RunStatus.completed, "")
            event_writer.emit("run_completed", {"run_id": run.id})
        except RunCancelled:
            self._set_run_state(db_session, run, RunStatus.cancelled, "")
            event_writer.emit("run_cancelled", {"run_id": run.id})
        except Exception as exc:  # noqa: BLE001
            error = normalize_runtime_error(exc)
            self._set_run_state(db_session, run, RunStatus.failed, error)
            event_writer.emit("run_failed", {"run_id": run.id, "error": error})

    def _stream_graph(
        self,
        *,
        db_session: Session,
        run: AgentRun,
        state: dict[str, Any],
        config: dict[str, Any],
        event_writer: AgentEventWriter,
        run_id: str,
    ) -> tuple[list[BaseMessage], str]:
        new_messages: list[BaseMessage] = []
        streamed_assistant_parts: list[str] = []
        last_streamed_message_id = ""

        for item in self.graph.stream(state, config=config, stream_mode=["messages", "updates"]):
            self._ensure_run_not_cancelled(db_session, run.id)
            self._heartbeat_run(db_session, run.id)
            if not (isinstance(item, tuple) and len(item) == 2):
                continue

            stream_mode, payload = item
            if stream_mode == "messages":
                last_streamed_message_id = self._emit_message_chunk(
                    payload=payload,
                    run_id=run_id,
                    event_writer=event_writer,
                    streamed_assistant_parts=streamed_assistant_parts,
                    last_streamed_message_id=last_streamed_message_id,
                )
                continue

            if stream_mode == "updates" and isinstance(payload, dict):
                new_messages.extend(_messages_from_update_payload(payload))

        return new_messages, "".join(streamed_assistant_parts)

    @staticmethod
    def _emit_message_chunk(
        *,
        payload: Any,
        run_id: str,
        event_writer: AgentEventWriter,
        streamed_assistant_parts: list[str],
        last_streamed_message_id: str,
    ) -> str:
        if not (isinstance(payload, tuple) and payload):
            return last_streamed_message_id
        message = payload[0]
        metadata = payload[1] if len(payload) > 1 and isinstance(payload[1], dict) else {}
        if metadata.get("langgraph_node") != "agent":
            return last_streamed_message_id
        if not isinstance(message, AIMessage):
            return last_streamed_message_id
        if getattr(message, "tool_calls", None) or getattr(message, "tool_call_chunks", None):
            return last_streamed_message_id

        chunk = message_to_text(message)
        if not chunk:
            return last_streamed_message_id

        message_id = getattr(message, "id", "") or ""
        streamed_text = "".join(streamed_assistant_parts)
        if message_id and message_id == last_streamed_message_id and chunk == streamed_text:
            return last_streamed_message_id

        streamed_assistant_parts.append(chunk)
        event_writer.emit(
            "assistant_message_delta",
            {
                "run_id": run_id,
                "message_type": MessageType.markdown,
                "chunk": chunk,
                "done": False,
            },
        )
        return message_id or last_streamed_message_id

    @staticmethod
    def _set_run_state(
        db_session: Session,
        run: AgentRun,
        status: RunStatus,
        error: str,
    ) -> None:
        now = now_utc()
        run.status = status
        run.error = error
        run.touch_updated_at(now)
        if status == RunStatus.running:
            run.started_at = run.started_at or now
            run.last_heartbeat_at = now
        if status in TERMINAL_RUN_STATUSES:
            run.finished_at = run.finished_at or now
            run.lease_owner = None
            run.lease_expires_at = None
        db_session.add(run)
        db_session.commit()

    @staticmethod
    def _ensure_run_not_cancelled(db_session: Session, run_id: str) -> None:
        with Session(db_session.get_bind()) as fresh:
            run = fresh.get(AgentRun, run_id)
            if run is not None and run.cancel_requested_at is not None:
                raise RunCancelled()

    @staticmethod
    def _heartbeat_run(db_session: Session, run_id: str) -> None:
        now = now_utc()
        with Session(db_session.get_bind()) as fresh:
            run = fresh.get(AgentRun, run_id)
            if run is None:
                return
            if run.last_heartbeat_at and now - run.last_heartbeat_at < timedelta(
                seconds=HEARTBEAT_INTERVAL_SECONDS
            ):
                return
            run.last_heartbeat_at = now
            run.touch_updated_at(now)
            fresh.add(run)
            fresh.commit()

    def _persist_graph_messages(
        self,
        db_session: Session,
        *,
        run: AgentRun,
        messages: list[BaseMessage],
        event_writer: AgentEventWriter,
        streamed_assistant_text: str = "",
    ) -> bool:
        assistant_saved = False
        for message in messages:
            if isinstance(message, HumanMessage):
                continue
            if isinstance(message, ToolMessage):
                self._persist_tool_message(
                    db_session,
                    run=run,
                    message=message,
                    event_writer=event_writer,
                )
                continue
            if isinstance(message, AIMessage):
                content = message_to_text(message)
                if not content or message.tool_calls:
                    continue
                self._persist_assistant_text(
                    db_session,
                    run=run,
                    content=content,
                    metadata={"run_id": run.id},
                    event_writer=event_writer,
                    emit_delta=not streamed_assistant_text,
                )
                assistant_saved = True
        return assistant_saved

    def _persist_tool_message(
        self,
        db_session: Session,
        *,
        run: AgentRun,
        message: ToolMessage,
        event_writer: AgentEventWriter,
    ) -> None:
        content = message_to_text(message)
        db_session.add(
            ChatMessage(
                session_id=run.session_id,
                role=MessageRole.tool,
                message_type=MessageType.json if looks_like_json(content) else MessageType.text,
                message_metadata=json_dumps({"run_id": run.id}),
                content=content[:240000],
                run_id=run.id,
                tool_name=message.name,
                tool_call_id=message.tool_call_id,
            )
        )
        db_session.commit()
        event_writer.emit(
            "tool_call_completed",
            {
                "run_id": run.id,
                "tool_name": message.name,
                "tool_call_id": message.tool_call_id,
            },
        )

    def _persist_assistant_text(
        self,
        db_session: Session,
        *,
        run: AgentRun,
        content: str,
        metadata: dict[str, Any],
        event_writer: AgentEventWriter,
        emit_delta: bool = True,
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
                session_id=run.session_id,
                role=MessageRole.assistant,
                message_type=MessageType.markdown,
                message_metadata=json_dumps(message_metadata),
                content=response.content,
                run_id=run.id,
            )
        )
        db_session.commit()
        event_writer.emit(
            "assistant_message_delta",
            {
                "run_id": run.id,
                "message_type": response.message_type,
                "chunk": response.content if emit_delta else "",
                "done": True,
            },
        )
        event_writer.emit(
            "assistant_message",
            {
                "run_id": run.id,
                "message_type": response.message_type,
                "content": response.content,
            },
        )


def _messages_from_update_payload(payload: dict[str, Any]) -> list[BaseMessage]:
    messages: list[BaseMessage] = []
    for update in payload.values():
        if not isinstance(update, dict):
            continue
        value = update.get("messages", [])
        if isinstance(value, BaseMessage):
            messages.append(value)
        elif isinstance(value, list):
            messages.extend(message for message in value if isinstance(message, BaseMessage))
    return messages


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


HTML_TAG_RE = re.compile(r"<[^>]+>")
MAX_ERROR_MESSAGE_LENGTH = 4000


def normalize_runtime_error(exc: Exception) -> str:
    message = _compact_error_text(str(exc))
    if not message:
        return "执行失败：模型服务未返回错误详情。"

    lowered = message.lower()
    if "<html" in lowered or "<!doctype html" in lowered:
        text = _compact_error_text(html.unescape(HTML_TAG_RE.sub(" ", message)))
        if "service suspended" in text.lower():
            return (
                "模型服务暂不可用：当前配置的模型网关服务已暂停。"
                "请检查 TRAFFIC_RELAY_BASE_URL / TRAFFIC_RELAY_API_KEY，"
                "并更换为可用的 OpenAI 兼容模型服务。"
            )
        return "模型服务返回了非 JSON/HTML 错误页面，请检查模型网关配置。"

    if "service suspended" in lowered:
        return (
            "模型服务暂不可用：当前配置的模型网关服务已暂停。"
            "请检查 TRAFFIC_RELAY_BASE_URL / TRAFFIC_RELAY_API_KEY。"
        )

    if "401" in lowered or "unauthorized" in lowered or "invalid api key" in lowered:
        return "模型服务认证失败：请检查 TRAFFIC_RELAY_API_KEY 是否有效。"

    if "403" in lowered or "forbidden" in lowered:
        return "模型服务拒绝访问：请检查模型网关权限、模型名称或 API Key 权限。"

    if "insufficient" in lowered or "quota" in lowered or "billing" in lowered:
        return "模型服务额度不足或计费异常：请检查模型服务账户额度。"

    return message[:MAX_ERROR_MESSAGE_LENGTH]


def _compact_error_text(value: str) -> str:
    return " ".join((value or "").strip().split())


def memory_payload(memories: list[MemoryEntry]) -> list[dict[str, Any]]:
    return [
        {
            "key": memory.key,
            "kind": memory.kind,
            "content": memory.content,
            "payload": memory.payload,
            "updated_at": memory.updated_at,
        }
        for memory in memories
    ]


agent_executor = AgentExecutor()
