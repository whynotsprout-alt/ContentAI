from __future__ import annotations

import json
from typing import Any

from agent.context.assembler import ContextAssembler
from agent.graph.factory import build_agent_graph
from agent.infrastructure.llm import model_gateway
from agent.memory import LongTermMemory, MemoryEntry, MemoryRepository, ShortTermMemory
from agent.runtime.checkpoint import build_checkpointer, build_store, checkpoint_messages
from agent.runtime.context import ToolRuntimeContext, tool_runtime_scope
from agent.runtime.events import AgentEventWriter, event_writer_scope, now_utc
from agent.tools.registry import build_tool_set, tool_names
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from models.account import Account
from models.base import json_dumps
from models.chat import AgentRun, ChatMessage
from models.enums import MessageRole, MessageType, RunStatus
from sqlmodel import Session

MAX_AGENT_ITERATIONS = 8
TERMINAL_RUN_STATUSES = {
    RunStatus.completed,
    RunStatus.failed,
    RunStatus.cancelled,
    RunStatus.interrupted,
}


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
                long_term_memory=long_term,
            )
            with event_writer_scope(event_writer), tool_runtime_scope(tool_context):
                result = self.graph.invoke(state, config=config)

            result_messages = result.get("messages", []) if isinstance(result, dict) else []
            new_messages = result_messages[before_count:]
            persisted_assistant = self._persist_graph_messages(
                db_session,
                run=run,
                messages=new_messages,
                event_writer=event_writer,
            )
            if not persisted_assistant:
                self._persist_assistant_text(
                    db_session,
                    run=run,
                    content="我已经完成处理，但模型没有返回可展示内容。",
                    metadata={"run_id": run.id, "fallback": True},
                    event_writer=event_writer,
                )

            short_term.refresh_summary(db_session, session_id=run.session_id)
            self._set_run_state(db_session, run, RunStatus.completed, "")
            event_writer.emit("run_completed", {"run_id": run.id})
        except Exception as exc:  # noqa: BLE001
            self._set_run_state(db_session, run, RunStatus.failed, str(exc))
            event_writer.emit("run_failed", {"run_id": run.id, "error": str(exc)})

    @staticmethod
    def _set_run_state(
        db_session: Session,
        run: AgentRun,
        status: RunStatus,
        error: str,
    ) -> None:
        run.status = status
        run.error = error
        run.touch_updated_at(now_utc())
        db_session.add(run)
        db_session.commit()

    def _persist_graph_messages(
        self,
        db_session: Session,
        *,
        run: AgentRun,
        messages: list[BaseMessage],
        event_writer: AgentEventWriter,
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
    ) -> None:
        db_session.add(
            ChatMessage(
                session_id=run.session_id,
                role=MessageRole.assistant,
                message_type=MessageType.markdown,
                message_metadata=json_dumps(metadata),
                content=content[:240000],
                run_id=run.id,
            )
        )
        db_session.commit()
        event_writer.emit(
            "assistant_message_delta",
            {
                "run_id": run.id,
                "message_type": MessageType.markdown,
                "chunk": content,
                "done": True,
            },
        )
        event_writer.emit(
            "assistant_message",
            {
                "run_id": run.id,
                "message_type": MessageType.markdown,
                "content": content,
            },
        )


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
