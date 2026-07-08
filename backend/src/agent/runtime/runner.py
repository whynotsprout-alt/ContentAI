from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime
from typing import Any

from memory import LongTermMemory, MemoryRepository, ShortTermMemory
from agent.runtime.checkpoint import checkpoint_messages
from agent.runtime.context import ToolRuntimeContext, tool_runtime_scope
from agent.runtime.errors import normalize_runtime_error
from agent.runtime.events import event_writer_scope, now_utc
from memory.execution_state import ExecutionCancelled, TERMINAL_RUN_STATUSES
from memory.message_persister import message_to_text
from agent.tools.registry import tool_names
from db.session import get_engine
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langgraph.types import Command
from models.account import Account
from models.chat import AgentExecution, AgentInvocation, ChatMessage, ChatSession
from models.enums import MessageType, RunStatus
from pydantic import BaseModel, Field
from sqlmodel import Session

LLM_TOKEN_ESTIMATE_CHARS_PER_TOKEN = 4
logger = logging.getLogger(__name__)


class AgentRunner:
    def __init__(self, container: Any) -> None:
        self.container = container
        self._background_threads: set[threading.Thread] = set()
        self._background_threads_lock = threading.Lock()
        self._closing = False

    def close(self) -> None:
        with self._background_threads_lock:
            self._closing = True
            threads = list(self._background_threads)
        for thread in threads:
            if thread.is_alive():
                thread.join(timeout=5)

    def run(
        self,
        db_session: Session,
        *,
        execution_id: str,
        event_writer: Any | None = None,
        tool_permissions: tuple[str, ...] = ("*",),
        resume_value: Any = None,
    ) -> None:
        loaded = self._load_execution_context(db_session, execution_id)
        if loaded is None:
            return
        execution, invocation, chat, user_message = loaded
        if execution.status in TERMINAL_RUN_STATUSES:
            return

        if event_writer is None:
            event_writer = self._build_event_writer(execution)
            owns_event_writer = True
        else:
            owns_event_writer = False

        state_manager = self.container.state_manager
        persister = self.container.message_persister

        try:
            state_manager.ensure_execution_not_cancelled(db_session, execution)
            state_manager.set_execution_state(db_session, execution, RunStatus.running, "")
            event_writer.emit("execution_started", {"execution_id": execution.id})
            execution_started_at = now_utc()
            _emit_perf_marker(
                event_writer=event_writer,
                execution_id=execution.id,
                marker="request_to_running_ms",
                requested_at=execution.created_at.isoformat(),
                running_at=execution_started_at.isoformat(),
                value=_utc_elapsed_ms(execution.created_at, execution_started_at),
            )

            account = db_session.get(Account, chat.account_id)
            if account is None:
                raise RuntimeError("Account not found for agent execution.")

            repository = MemoryRepository(db_session)
            short_term = ShortTermMemory(repository)
            long_term = LongTermMemory(repository, self.container.store)

            short_summary, db_messages = short_term.load(
                db_session,
                session_id=chat.id,
                tenant_id=chat.tenant_id,
                user_id=chat.owner_user_id,
                account_id=chat.account_id,
            )
            recalled = long_term.recall(
                chat.account_id,
                user_message.content,
                tenant_id=invocation.tenant_id,
                user_id=invocation.created_by_user_id,
                limit=8,
            )
            agent_context = self.container.assembler.assemble(
                account=account,
                messages=db_messages,
                short_term_summary=short_summary,
                long_term_memories=recalled,
                tool_names=tool_names(self.container.tools),
                focus_message=user_message.content,
            )

            checkpoint_history = checkpoint_messages(
                self.container.checkpointer,
                thread_id=chat.langgraph_thread_id,
            )
            graph_input, _ = self._graph_input(
                resume_value=resume_value,
                user_message=user_message.content,
                checkpoint_history=checkpoint_history,
                agent_messages=agent_context.messages,
            )
            state = {
                "messages": graph_input,
                "system_prompt": agent_context.system_prompt,
                "iterations": 0,
                "max_iterations": self.container.settings.agent.max_iterations,
            }
            config = {
                "configurable": {"thread_id": chat.langgraph_thread_id},
                "recursion_limit": self.container.settings.agent.recursion_limit,
            }
            tool_context = ToolRuntimeContext(
                execution_id=execution.id,
                session_id=chat.id,
                account_id=chat.account_id,
                tenant_id=invocation.tenant_id,
                user_id=invocation.created_by_user_id,
                allowed_hotspot_sources=account.hotspot_sources,
                tool_permissions=tool_permissions or ("*",),
                long_term_memory=long_term,
            )

            with event_writer_scope(event_writer), tool_runtime_scope(tool_context):
                new_messages, streamed_assistant_text, interrupt_payload = self._stream_graph(
                    db_session=db_session,
                    execution_id=execution.id,
                    execution=execution,
                    graph_input=graph_input,
                    state=state,
                    config=config,
                    event_writer=event_writer,
                )

            if interrupt_payload is not None:
                state_manager.mark_interrupted(db_session, execution)
                event_writer.emit(
                    "execution_interrupted",
                    {"execution_id": execution.id, "interrupt": interrupt_payload},
                )
                return

            assistant_text = _assistant_text_from_messages(new_messages) or streamed_assistant_text

            persist_started_at = time.perf_counter()
            persisted_assistant = persister.persist_graph_messages(
                db_session,
                session_id=chat.id,
                invocation_id=invocation.id,
                execution_id=execution.id,
                messages=new_messages,
                event_writer=event_writer,
                streamed_assistant_text=streamed_assistant_text,
            )
            _emit_perf_marker(
                event_writer=event_writer,
                execution_id=execution.id,
                marker="persist_messages_ms",
                value=int((time.perf_counter() - persist_started_at) * 1000),
                messages_count=len(new_messages),
                persisted_assistant=bool(persisted_assistant),
            )
            if not persisted_assistant:
                fallback_started_at = time.perf_counter()
                if not assistant_text:
                    assistant_text = (
                        "The request completed, but the model did not return "
                        "displayable content."
                    )
                persister.persist_assistant_text(
                    db_session,
                    session_id=chat.id,
                    invocation_id=invocation.id,
                    execution_id=execution.id,
                    content=assistant_text,
                    metadata={
                        "execution_id": execution.id,
                        "invocation_id": invocation.id,
                        "fallback": True,
                    },
                    event_writer=event_writer,
                    emit_delta=not streamed_assistant_text,
                )
                _emit_perf_marker(
                    event_writer=event_writer,
                    execution_id=execution.id,
                    marker="persist_assistant_fallback_ms",
                    value=int((time.perf_counter() - fallback_started_at) * 1000),
                )

            state_manager.set_execution_state(db_session, execution, RunStatus.completed, "")
            try:
                event_writer.emit("execution_completed", {"execution_id": execution.id})
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to emit execution_completed for %s.",
                    execution.id,
                    exc_info=True,
                )

            try:
                tool_results = _tool_result_summaries(new_messages)
                if self.container.settings.agent.memory_after_turn_enabled:
                    self._extract_memory_after_turn(
                        execution_id=execution.id,
                        account_id=chat.account_id,
                        tenant_id=invocation.tenant_id,
                        user_id=invocation.created_by_user_id,
                        session_id=chat.id,
                        account_name=account.name,
                        account_positioning=account.positioning,
                        user_message=user_message.content,
                        user_message_id=user_message.id,
                        assistant_text=assistant_text,
                        tool_results=tool_results,
                        model_gateway=self.container.model_gateway,
                        store=self.container.store,
                        settings=self.container.settings,
                    )
                    _emit_perf_marker(
                        event_writer=event_writer,
                        execution_id=execution.id,
                        marker="memory_extraction_completed",
                    )
                else:
                    _emit_perf_marker(
                        event_writer=event_writer,
                        execution_id=execution.id,
                        marker="memory_extraction_skipped",
                        reason="disabled",
                    )

                self._schedule_short_summary_refresh(chat.id, self.container.settings)
                self._schedule_title_update(chat.id, user_message.content, self.container.settings)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Post-execution task scheduling failed for %s.",
                    execution.id,
                    exc_info=True,
                )
        except ExecutionCancelled:
            state_manager.set_execution_state(db_session, execution, RunStatus.cancelled, "")
            event_writer.emit("execution_cancelled", {"execution_id": execution.id})
        except Exception as exc:  # noqa: BLE001
            logger.exception("Agent execution failed: %s", execution.id)
            error = normalize_runtime_error(exc)
            state_manager.set_execution_state(db_session, execution, RunStatus.failed, error)
            event_writer.emit("execution_failed", {"execution_id": execution.id, "error": error})
        finally:
            if owns_event_writer:
                close_writer = getattr(event_writer, "close", None)
                if callable(close_writer):
                    close_writer()

    def _load_execution_context(
        self,
        db_session: Session,
        execution_id: str,
    ) -> tuple[AgentExecution, AgentInvocation, ChatSession, ChatMessage] | None:
        execution = db_session.get(AgentExecution, execution_id)
        if execution is None:
            return None
        invocation = db_session.get(AgentInvocation, execution.invocation_id)
        if invocation is None:
            return None
        chat = db_session.get(ChatSession, invocation.session_id)
        if chat is None:
            return None
        user_message = (
            db_session.get(ChatMessage, invocation.user_message_id)
            if invocation.user_message_id
            else None
        )
        if user_message is None:
            return None
        return execution, invocation, chat, user_message

    def _build_event_writer(self, execution: AgentExecution):
        from agent.runtime.events import AgentEventWriter
        return AgentEventWriter(execution.id)

    def _extract_memory_after_turn(
        self,
        *,
        execution_id: str,
        account_id: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
        account_name: str,
        account_positioning: str,
        user_message: str,
        user_message_id: str,
        assistant_text: str,
        tool_results: list[str],
        model_gateway: Any,
        store: Any,
        settings: Any,
    ) -> None:
        _extract_memory_background(
            execution_id=execution_id,
            account_id=account_id,
            tenant_id=tenant_id,
            user_id=user_id,
            session_id=session_id,
            account_name=account_name,
            account_positioning=account_positioning,
            user_message=user_message,
            user_message_id=user_message_id,
            assistant_text=assistant_text,
            tool_results=tool_results,
            model_gateway=model_gateway,
            store=store,
            settings=settings,
        )

    def _schedule_short_summary_refresh(self, session_id: str, settings: Any) -> None:
        self._start_background_thread(
            name=f"summary-refresh-{session_id}",
            target=_refresh_short_summary_background,
            kwargs={"session_id": session_id, "settings": settings},
        )

    def _schedule_title_update(self, session_id: str, user_message: str, settings: Any) -> None:
        self._start_background_thread(
            name=f"title-update-{session_id}",
            target=_update_title_background,
            kwargs={
                "session_id": session_id,
                "user_message": user_message,
                "settings": settings,
                "model_gateway": self.container.model_gateway,
            },
        )

    def _start_background_thread(
        self,
        *,
        name: str,
        target: Any,
        kwargs: dict[str, Any],
    ) -> None:
        with self._background_threads_lock:
            if self._closing:
                return

        def run_target() -> None:
            try:
                target(**kwargs)
            finally:
                with self._background_threads_lock:
                    self._background_threads.discard(threading.current_thread())

        thread = threading.Thread(target=run_target, name=name, daemon=True)
        with self._background_threads_lock:
            self._background_threads.add(thread)
        thread.start()

    def _graph_input(
        self,
        *,
        resume_value: Any,
        user_message: str,
        checkpoint_history: list[BaseMessage],
        agent_messages: list[BaseMessage],
    ) -> tuple[Any, int]:
        if resume_value is not None:
            return Command(resume=resume_value), len(checkpoint_history)
        if checkpoint_history:
            return [HumanMessage(content=user_message)], len(checkpoint_history)
        return agent_messages, len(agent_messages)

    def _stream_graph(
        self,
        *,
        db_session: Session | None,
        execution_id: str,
        graph_input: Any,
        state: dict[str, Any],
        config: dict[str, Any],
        event_writer: Any,
        execution: AgentExecution | None = None,
    ) -> tuple[list[BaseMessage], str, dict[str, Any] | None]:
        stream_input = graph_input if isinstance(graph_input, Command) else state
        new_messages: list[BaseMessage] = []
        streamed_assistant_parts: list[str] = []
        streamed_assistant_by_id: dict[str, str] = {}
        stream_message_id: str = ""

        graph_started_at = time.perf_counter()
        graph_iterations = 0
        current_node: str | None = None
        current_node_started_at = graph_started_at

        llm_started_at: float | None = None
        llm_message_id: str = ""
        llm_chars_in_span = 0
        llm_total_ms = 0
        llm_total_chars = 0
        llm_spans = 0

        for item in self.container.graph.stream(
            stream_input,
            config=config,
            stream_mode=["messages", "updates"],
        ):
            if db_session is not None and execution is not None:
                self.container.state_manager.ensure_execution_not_cancelled(
                    db_session,
                    execution,
                )
            if not isinstance(item, tuple) or len(item) != 2:
                continue
            stream_mode, payload = item

            now = time.perf_counter()
            if stream_mode == "messages":
                stream_message_id, chunk = _emit_message_chunk(
                    payload=payload,
                    execution_id=execution_id,
                    event_writer=event_writer,
                    streamed_assistant_parts=streamed_assistant_parts,
                    streamed_assistant_by_id=streamed_assistant_by_id,
                )
                if chunk:
                    if llm_started_at is None or llm_message_id != stream_message_id:
                        if llm_started_at is not None:
                            llm_total_ms += _emit_llm_inference_metric(
                                event_writer=event_writer,
                                execution_id=execution_id,
                                started_at=llm_started_at,
                                output_chars=llm_chars_in_span,
                            )
                            llm_spans += 1
                            llm_total_chars += llm_chars_in_span
                        llm_started_at = now
                        llm_message_id = stream_message_id
                        llm_chars_in_span = 0
                    llm_chars_in_span += len(chunk)
            elif stream_mode == "updates" and isinstance(payload, dict):
                interrupt_payload = _interrupt_payload(payload)
                if interrupt_payload is not None:
                    if llm_started_at is not None:
                        llm_total_ms += _emit_llm_inference_metric(
                            event_writer=event_writer,
                            execution_id=execution_id,
                            started_at=llm_started_at,
                            output_chars=llm_chars_in_span,
                            force=True,
                        )
                        llm_spans += 1
                        llm_total_chars += llm_chars_in_span
                    _emit_perf_marker(
                        event_writer=event_writer,
                        execution_id=execution_id,
                        marker="llm_total_inference_ms",
                        value=llm_total_ms,
                        estimated_output_tokens=int(
                            llm_total_chars / LLM_TOKEN_ESTIMATE_CHARS_PER_TOKEN
                        ),
                        spans=llm_spans,
                    )
                    return new_messages, "".join(streamed_assistant_parts), interrupt_payload

                update_nodes = _extract_node_names(payload)
                update_messages = _messages_from_update_payload(payload)
                if update_messages:
                    update_text = _assistant_text_from_messages(update_messages)
                    if update_text and not streamed_assistant_parts:
                        streamed_assistant_parts.append(update_text)

                if update_nodes:
                    graph_iterations += 1
                    next_node = update_nodes[-1]
                    if current_node is None:
                        current_node = next_node
                        current_node_started_at = now
                        _emit_perf_marker(
                            event_writer=event_writer,
                            execution_id=execution_id,
                            marker="graph_node_started",
                            node=next_node,
                            iteration=graph_iterations,
                        )
                    elif next_node != current_node:
                        _emit_perf_marker(
                            event_writer=event_writer,
                            execution_id=execution_id,
                            marker="graph_node_switch_ms",
                            from_node=current_node,
                            to_node=next_node,
                            duration_ms=int((now - current_node_started_at) * 1000),
                            iteration=graph_iterations,
                        )
                        current_node = next_node
                        current_node_started_at = now
                new_messages.extend(update_messages)

        final_time = time.perf_counter()
        if llm_started_at is not None:
            llm_total_ms += _emit_llm_inference_metric(
                event_writer=event_writer,
                execution_id=execution_id,
                started_at=llm_started_at,
                output_chars=llm_chars_in_span,
                force=True,
            )
            llm_spans += 1
            llm_total_chars += llm_chars_in_span
            _emit_perf_marker(
                event_writer=event_writer,
                execution_id=execution_id,
                marker="llm_total_inference_ms",
                value=llm_total_ms,
                estimated_output_tokens=int(
                    llm_total_chars / LLM_TOKEN_ESTIMATE_CHARS_PER_TOKEN
                ),
                spans=llm_spans,
            )
        if current_node:
            _emit_perf_marker(
                event_writer=event_writer,
                execution_id=execution_id,
                marker="graph_node_final_ms",
                node=current_node,
                duration_ms=int((final_time - current_node_started_at) * 1000),
                iterations=graph_iterations,
            )
        _emit_perf_marker(
            event_writer=event_writer,
            execution_id=execution_id,
            marker="graph_runtime_ms",
            value=int((final_time - graph_started_at) * 1000),
            iterations=graph_iterations,
        )
        return new_messages, "".join(streamed_assistant_parts), None


def _extract_memory_background(
    *,
    execution_id: str,
    account_id: str,
    tenant_id: str,
    user_id: str,
    session_id: str,
    account_name: str,
    account_positioning: str,
    user_message: str,
    user_message_id: str,
    assistant_text: str,
    tool_results: list[str],
    model_gateway: Any,
    store: Any,
    settings: Any,
) -> None:
    from agent.runtime.events import AgentEventWriter

    started_at = time.perf_counter()
    with Session(get_engine(settings)) as session:
        writer = AgentEventWriter(execution_id)
        try:
            repository = MemoryRepository(session)
            long_term = LongTermMemory(repository, store)
            extracted = long_term.remember_after_turn(
                account_id=account_id,
                tenant_id=tenant_id,
                user_id=user_id,
                session_id=session_id,
                account_name=account_name,
                account_positioning=account_positioning,
                user_message=user_message,
                source_message_id=user_message_id,
                source_execution_id=execution_id,
                assistant_response=assistant_text,
                tool_results=tool_results,
                model_gateway=model_gateway,
            )
            _emit_perf_marker(
                event_writer=writer,
                execution_id=execution_id,
                marker="memory_extraction_ms",
                value=int((time.perf_counter() - started_at) * 1000),
                memories=len(extracted),
            )
            writer.emit(
                "memory_extracted",
                {
                    "execution_id": execution_id,
                    "count": len(extracted),
                    "memories": [
                        {
                            "key": item.key,
                            "kind": item.kind,
                            "content": item.content,
                        }
                        for item in extracted
                    ],
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Background memory extraction failed for execution %s.",
                execution_id,
                exc_info=True,
            )
            _emit_perf_marker(
                event_writer=writer,
                execution_id=execution_id,
                marker="memory_extraction_ms",
                value=int((time.perf_counter() - started_at) * 1000),
                failed=True,
            )
            writer.emit(
                "memory_extraction_failed",
                {"execution_id": execution_id, "error": str(exc)[:800]},
            )
        finally:
            writer.close()


def _refresh_short_summary_background(*, session_id: str, settings: Any) -> None:
    try:
        with Session(get_engine(settings)) as session:
            chat = session.get(ChatSession, session_id)
            if chat is None:
                return
            repository = MemoryRepository(session)
            ShortTermMemory(repository).refresh_summary(
                session,
                session_id=session_id,
                tenant_id=chat.tenant_id,
                user_id=chat.owner_user_id,
                account_id=chat.account_id,
            )
    except Exception:  # noqa: BLE001
        logger.warning(
            "Background short-term summary refresh failed for %s.",
            session_id,
            exc_info=True,
        )


def _update_title_background(
    *,
    session_id: str,
    user_message: str,
    settings: Any,
    model_gateway: Any | None = None,
) -> None:
    try:
        with Session(get_engine(settings)) as session:
            chat = session.get(ChatSession, session_id)
            if chat is None:
                return

            if chat.title != "New Session":
                return
            chat.title = _generate_session_title(
                user_message=user_message,
                model_gateway=model_gateway,
            )
            chat.touch_updated_at()
            session.add(chat)
            session.commit()
    except Exception:  # noqa: BLE001
        logger.warning("Background title update failed for %s.", session_id, exc_info=True)


def _generate_session_title(*, user_message: str, model_gateway: Any | None) -> str:
    fallback = _fallback_session_title(user_message)
    if model_gateway is None:
        return fallback
    prompt = (
        "Generate a concise chat title for this user message.\n"
        "Return at most 15 characters without markdown.\n"
        f"Message:\n{user_message[:1200]}"
    )
    try:
        result = model_gateway.build_structured_output_model(SessionTitleResult).invoke(prompt)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Session title generation failed: %s", exc)
        return fallback

    if isinstance(result, SessionTitleResult):
        raw_title = result.title
    elif isinstance(result, dict):
        raw_title = result.get("title", "")
    else:
        raw_title = str(getattr(result, "title", "") or "")
    normalized = _normalize_title(raw_title)
    return normalized or fallback


class SessionTitleResult(BaseModel):
    title: str = Field(default="", max_length=60)


def _normalize_title(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip())[:15]


def _fallback_session_title(content: str) -> str:
    normalized = _normalize_title(content)
    if not normalized:
        return "New Session"
    return normalized


def _emit_perf_marker(event_writer: Any, execution_id: str, marker: str, **payload: Any) -> None:
    event_writer.emit(
        "agent_runtime_marker",
        {"execution_id": execution_id, "marker": marker, **payload},
    )


def _emit_llm_inference_metric(
    *,
    event_writer: Any,
    execution_id: str,
    started_at: float,
    output_chars: int,
    force: bool = False,
) -> int:
    if not force and started_at <= 0:
        return 0
    now = time.perf_counter()
    elapsed_ms = int((now - started_at) * 1000)
    if elapsed_ms <= 0:
        return 0
    output_tokens = int(output_chars / LLM_TOKEN_ESTIMATE_CHARS_PER_TOKEN)
    tokens_per_second = output_tokens / (elapsed_ms / 1000) if elapsed_ms > 0 else 0.0
    _emit_perf_marker(
        event_writer=event_writer,
        execution_id=execution_id,
        marker="llm_inference_ms",
        value=elapsed_ms,
        output_chars=output_chars,
        estimated_output_tokens=output_tokens,
        estimated_tokens_per_second=tokens_per_second,
    )
    return elapsed_ms


def _extract_node_names(payload: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for key, value in payload.items():
        if not isinstance(key, str) or key == "__interrupt__":
            continue
        if not isinstance(value, dict):
            continue
        names.append(key)
    return names


def _message_text_delta(
    *,
    previous: str,
    current: str,
) -> str:
    if not current:
        return ""
    if not previous:
        return current
    if current == previous:
        return ""
    if current.startswith(previous):
        return current[len(previous) :]
    if previous.endswith(current):
        return ""
    overlap = _suffix_prefix_overlap(previous, current)
    if overlap:
        return current[overlap:]
    if current.endswith(previous):
        return ""
    return current


def _suffix_prefix_overlap(previous: str, current: str) -> int:
    max_overlap = min(len(previous), len(current))
    for size in range(max_overlap, 0, -1):
        if previous[-size:] == current[:size]:
            return size
    return 0


def _emit_message_chunk(
    *,
    payload: Any,
    execution_id: str,
    event_writer: Any,
    streamed_assistant_parts: list[str],
    streamed_assistant_by_id: dict[str, str],
) -> tuple[str, str]:
    message, metadata = _extract_message_payload(payload)
    if message is None:
        return "", ""
    if metadata.get("langgraph_node") not in (None, "agent"):
        return getattr(message, "id", "") or "", ""
    if not isinstance(message, AIMessageChunk):
        return "", ""

    is_tool_message = bool(
        getattr(message, "tool_calls", None)
        or getattr(message, "tool_call_chunks", None)
    )
    if is_tool_message:
        return getattr(message, "id", "") or "", ""

    chunk_text = _message_chunk_text(message)
    if not chunk_text:
        return getattr(message, "id", "") or "", ""

    message_id = getattr(message, "id", "") or ""
    cache_key = "__agent_assistant_text__"
    normalized = streamed_assistant_by_id.get(cache_key, "")
    delta = _message_text_delta(previous=normalized, current=chunk_text)
    if not delta:
        return message_id, ""

    streamed_assistant_by_id[cache_key] = normalized + delta

    streamed_assistant_parts.append(delta)
    event_writer.emit(
        "assistant_message_delta",
        {
            "execution_id": execution_id,
            "message_type": MessageType.markdown,
            "chunk": delta,
            "done": False,
        },
    )
    return message_id, delta


def _extract_message_payload(
    payload: Any,
) -> tuple[BaseMessage | None, dict[str, Any]]:
    if isinstance(payload, BaseMessage):
        return payload, {}
    if isinstance(payload, tuple):
        candidate = payload[0] if payload else None
        if isinstance(candidate, BaseMessage):
            metadata = payload[1] if len(payload) > 1 and isinstance(payload[1], dict) else {}
            return candidate, metadata
        if isinstance(candidate, list | tuple):
            return _extract_message_payload(candidate)
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, BaseMessage):
                return item, {}
            if isinstance(item, tuple):
                message, metadata = _extract_message_payload(item)
                if message is not None:
                    return message, metadata
    return None, {}


def _message_chunk_text(message: AIMessageChunk) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return str(content)


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


def _assistant_text_from_messages(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and not getattr(message, "tool_calls", None):
            content = message_to_text(message)
            if content:
                return content
    return ""


def _tool_result_summaries(messages: list[BaseMessage]) -> list[str]:
    summaries: list[str] = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        content = message_to_text(message)
        if content:
            tool_name = message.name or "tool"
            summaries.append(f"{tool_name}: {content[:1200]}")
    return summaries[:10]


def _interrupt_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    interrupts = payload.get("__interrupt__")
    if not interrupts:
        return None
    normalized: list[dict[str, Any]] = []
    for interrupt in interrupts if isinstance(interrupts, tuple | list) else [interrupts]:
        normalized.append(
            {
                "id": str(getattr(interrupt, "id", "")),
                "value": getattr(interrupt, "value", interrupt),
            }
        )
    return {"interrupts": normalized}


def _utc_elapsed_ms(started_at: datetime | None, ended_at: datetime | None) -> int:
    if started_at is None or ended_at is None:
        return 0
    try:
        return max(int((ended_at - started_at).total_seconds() * 1000), 0)
    except Exception:
        return 0
