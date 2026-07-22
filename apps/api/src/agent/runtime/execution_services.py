from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from agent.context.assembler import AgentContext, ContextAssembler
from agent.runtime.checkpoint import checkpoint_messages
from agent.runtime.context import ToolRuntimeContext, tool_runtime_scope
from agent.runtime.errors import classify_runtime_error
from agent.runtime.events import (
    PersistentAgentEventWriter,
    now_utc,
)
from agent.runtime.turn_context import (
    DurableTurnContext,
)
from agent.workflows.deep_research import ContentEvidenceInvalidError
from agent.workflows.final_evidence import (
    build_supported_research_evidence,
    parse_research_identity,
    validate_research_final_proof,
)
from agent.workflows.research_repository import ResearchPackageRepository
from core.hotspot_sources import DEFAULT_HOTSPOT_SOURCES, partition_hotspot_sources
from db.session import get_engine
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command
from memory import LongTermMemory, MemoryRepository, ShortTermMemory
from memory.execution_state import ExecutionStateManager
from memory.message_persister import MessagePersister, message_to_text
from models.agent import AgentProfile, AgentVersion
from models.base import utcnow
from models.chat import AgentExecution, AgentInvocation, ChatMessage, ChatSession, ExecutionOutbox
from models.enums import MessageRole, MessageType
from pydantic import BaseModel, Field
from services.usage_service import ModelUsageCallback, UsageContext
from sqlmodel import Session, select

LLM_TOKEN_ESTIMATE_CHARS_PER_TOKEN = 4
logger = logging.getLogger(__name__)


def _execution_research_package(
    db_session: Session,
    *,
    execution_id: str,
    graph: Any,
    config: dict[str, Any],
) -> Any | None:
    identity = _execution_research_identity(graph, config)
    if identity is None:
        return None
    package_id, topic_hash = identity
    return ResearchPackageRepository.for_execution(
        db_session,
        package_id=package_id,
        execution_id=execution_id,
        topic_hash=topic_hash,
    )


def _execution_research_identity(
    graph: Any,
    config: dict[str, Any],
) -> tuple[str, str] | None:
    snapshot = graph.get_state(config)
    values = getattr(snapshot, "values", None)
    if not isinstance(values, dict):
        return None
    return parse_research_identity(
        values.get("research_package_id"),
        values.get("research_topic_hash"),
    )


@dataclass(frozen=True)
class ExecutionTurnResult:
    new_messages: list[BaseMessage]
    assistant_text: str
    streamed_assistant_text: str
    interrupt_payload: dict[str, Any] | None
    assistant_message: ChatMessage | None = None


class AgentRuntimeEventService:
    def __init__(self, settings: Any | None = None) -> None:
        self._settings = settings

    def new_writer(
        self,
        db_session: Session,
        execution: AgentExecution,
        *,
        settings: Any | None = None,
        trace_id: str | None = None,
        thread_id: str | None = None,
        request_id: str | None = None,
        conversation_id: str | None = None,
    ) -> PersistentAgentEventWriter:
        writer_settings = self._settings if settings is None else settings
        return PersistentAgentEventWriter(
            execution.id,
            db_session.get_bind(),
            settings=writer_settings,
            trace_id=trace_id,
            thread_id=thread_id,
            request_id=request_id,
            conversation_id=conversation_id,
        )

    def emit_execution_started(self, event_writer: Any, execution: AgentExecution) -> None:
        if execution.attempt_count == 1:
            event_writer.emit("run_start", {"execution_id": execution.id})
        elif str(execution.next_attempt_kind) == "retry":
            event_writer.emit("run_retry", {"execution_id": execution.id})
        else:
            event_writer.emit("run_resume", {"execution_id": execution.id})
        event_writer.emit(
            "attempt_start",
            {
                "execution_id": execution.id,
                "attempt_id": execution.current_attempt_id,
                "attempt": execution.attempt_count,
            },
        )
        execution_started_at = now_utc()
        self.emit_marker(
            event_writer=event_writer,
            execution_id=execution.id,
            marker="request_to_running_ms",
            requested_at=execution.created_at.isoformat(),
            running_at=execution_started_at.isoformat(),
            value=_utc_elapsed_ms(execution.created_at, execution_started_at),
        )

    def emit_execution_completed(self, event_writer: Any, execution: AgentExecution) -> None:
        try:
            event_writer.emit("message_finish", {"execution_id": execution.id})
            event_writer.emit(
                "attempt_end",
                {
                    "execution_id": execution.id,
                    "attempt_id": execution.current_attempt_id,
                    "status": "completed",
                },
            )
            event_writer.emit("run_finish", {"execution_id": execution.id})
        except Exception:  # noqa: BLE001
            logger.warning(
                "Failed to emit execution_completed for %s.",
                execution.id,
                exc_info=True,
            )

    def emit_execution_cancelled(self, event_writer: Any, execution: AgentExecution) -> None:
        event_writer.emit(
            "attempt_end",
            {
                "execution_id": execution.id,
                "attempt_id": execution.current_attempt_id,
                "status": "cancelled",
            },
        )
        event_writer.emit("run_cancel", {"execution_id": execution.id})

    def emit_execution_failed(
        self,
        event_writer: Any,
        execution: AgentExecution,
        error: str,
        *,
        error_code: str | None = None,
        retryable: bool = False,
    ) -> None:
        event_writer.emit(
            "attempt_end",
            {
                "execution_id": execution.id,
                "attempt_id": execution.current_attempt_id,
                "status": "failed",
            },
        )
        event_writer.emit(
            "run_error",
            {
                "execution_id": execution.id,
                "error": error,
                "error_code": error_code,
                "retryable": retryable,
            },
        )

    def emit_waiting_input(
        self,
        event_writer: Any,
        execution: AgentExecution,
        interrupt: dict[str, Any],
    ) -> None:
        event_writer.emit(
            "attempt_end",
            {
                "execution_id": execution.id,
                "attempt_id": execution.current_attempt_id,
                "status": "waiting_input",
            },
        )
        event_writer.emit(
            "run_interrupt",
            {"execution_id": execution.id, "interrupt": interrupt},
        )

    def emit_marker(
        self,
        *,
        event_writer: Any,
        execution_id: str,
        marker: str,
        **payload: Any,
    ) -> None:
        event_writer.emit(
            "agent_runtime_marker",
            {"execution_id": execution_id, "marker": marker, **payload},
        )

    def emit_llm_inference_metric(
        self,
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
        self.emit_marker(
            event_writer=event_writer,
            execution_id=execution_id,
            marker="llm_inference_ms",
            value=elapsed_ms,
            output_chars=output_chars,
            estimated_output_tokens=output_tokens,
            estimated_tokens_per_second=tokens_per_second,
        )
        return elapsed_ms


class AgentExecutionEngine:
    def __init__(
        self,
        container: Any,
        state_manager: ExecutionStateManager,
        context_assembler: ContextAssembler | None = None,
        message_persister: MessagePersister | None = None,
    ) -> None:
        self.container = container
        self.state_manager = state_manager
        runtime_settings = getattr(self.container, "settings", None)
        self.context_assembler = context_assembler or ContextAssembler(settings=runtime_settings)
        self.message_persister = message_persister or MessagePersister()

    def run_turn(
        self,
        *,
        db_session: Session,
        execution: AgentExecution,
        invocation: AgentInvocation,
        chat: ChatSession,
        user_message: ChatMessage,
        tool_permissions: tuple[str, ...],
        resume_value: Any,
        turn_context: DurableTurnContext,
        continue_from_checkpoint: bool = False,
        event_writer: Any,
        event_service: AgentRuntimeEventService,
        request_id: str | None = None,
        expected_worker_id: str | None = None,
    ) -> ExecutionTurnResult:
        agent_profile = db_session.get(AgentProfile, invocation.agent_id)
        if agent_profile is None:
            raise RuntimeError("Content account not found for agent execution.")
        agent_version = db_session.get(AgentVersion, execution.agent_version_id)
        if agent_version is None:
            raise RuntimeError("Agent version not found for agent execution.")

        allowed_hotspot_sources = _allowed_hotspot_sources(agent_version)
        runtime = self.container.create_runtime(
            model_config_id=execution.model_config_id,
            tool_permissions=tool_permissions,
            user_id=invocation.user_id,
            agent_id=chat.agent_id,
            session_id=chat.langgraph_thread_id,
            conversation_id=chat.id,
            execution_id=execution.id,
        )
        tool_names = list(runtime.tool_permissions)
        if tool_permissions != turn_context.tool_permissions:
            raise RuntimeError("TURN_CONTEXT_SNAPSHOT_INVALID")
        agent_context = AgentContext(
            system_prompt=turn_context.system_prompt,
            messages=turn_context.messages,
            short_term_summary="",
            long_term_memories=[],
            user_id=invocation.user_id,
            agent_id=chat.agent_id,
            conversation_id=chat.id,
            run_id=execution.id,
            permissions=tool_names,
        )

        graph_input = self._build_graph_input(
            resume_value=resume_value,
            continue_from_checkpoint=continue_from_checkpoint,
            agent_messages=agent_context.messages,
            system_prompt=agent_context.system_prompt,
        )
        state = {
            "messages": agent_context.messages if graph_input is None else graph_input,
            "available_tool_names": list(runtime.tool_permissions),
            "confirmation_tool_names": _confirmation_tool_names(
                self.container.tool_registry.registrations
            ),
            "task_status": "thinking",
        }
        tool_context = ToolRuntimeContext(
            execution_id=execution.id,
            conversation_id=chat.id,
            session_id=chat.langgraph_thread_id,
            agent_id=chat.agent_id,
            agent_version_id=agent_version.id,
            user_id=invocation.user_id,
            allowed_hotspot_sources=allowed_hotspot_sources,
            topic_scoring_prompt=agent_version.topic_scoring_prompt,
            hotspot_filter_model=runtime.gateway.build_hotspot_filter_model(),
            research_model_gateway=runtime.gateway,
            event_writer=event_writer,
            tool_policies={
                registration.name: {
                    "version": registration.version,
                    "timeout_seconds": registration.timeout_seconds,
                    "max_output_chars": registration.max_output_chars,
                    "side_effecting": registration.side_effecting,
                    "execution_mode": registration.execution_mode,
                }
                for registration in self.container.tool_registry.registrations
            },
            permissions=tuple(
                (
                    *runtime.tool_permissions,
                    "search_metaso_sources",
                    "search_anspire_sources",
                )
            ),
            api_keys=self._build_tool_api_keys(),
            long_term_memory=LongTermMemory(MemoryRepository(db_session)),
            side_effect_dispatcher=self.container.side_effect_dispatcher,
            side_effect_receipt_poller=self.container.side_effect_receipt_poller,
            cancellation_check=lambda: self.state_manager.ensure_execution_not_cancelled(
                db_session,
                execution,
                expected_worker_id=expected_worker_id,
            ),
        )
        stream_graph_config = dict(runtime.config)
        configurable = stream_graph_config.get("configurable")
        if not isinstance(configurable, dict):
            configurable = {}
        configurable = dict(configurable)
        configurable.update(tool_context.as_graph_configurable())
        stream_graph_config["configurable"] = configurable

        def run_general_chat() -> tuple[list[BaseMessage], str, dict[str, Any] | None]:
            callbacks = list(stream_graph_config.get("callbacks") or [])
            callbacks.append(
                ModelUsageCallback(
                    self.container.settings,
                    UsageContext(
                        user_id=invocation.user_id,
                        session_id=chat.id,
                        execution_id=execution.id,
                        category="chat_agent",
                    ),
                )
            )
            stream_graph_config["callbacks"] = callbacks
            with tool_runtime_scope(tool_context):
                return self._stream_graph(
                    db_session=db_session,
                    execution=execution,
                    execution_id=execution.id,
                    event_writer=event_writer,
                    event_service=event_service,
                    graph=runtime.graph,
                    graph_input=graph_input,
                    state=state,
                    config=stream_graph_config,
                    expected_worker_id=expected_worker_id,
                )

        event_service.emit_marker(
            event_writer=event_writer,
            execution_id=execution.id,
            marker="agent_graph_started",
        )
        new_messages, streamed_assistant_text, interrupt_payload = run_general_chat()

        if interrupt_payload is not None:
            return ExecutionTurnResult(
                new_messages=[],
                assistant_text="",
                streamed_assistant_text="",
                interrupt_payload=interrupt_payload,
                assistant_message=None,
            )

        durable_messages = checkpoint_messages(
            runtime.checkpointer,
            thread_id=chat.langgraph_thread_id,
            execution_id=execution.id,
        )
        if durable_messages:
            new_messages = durable_messages

        assistant_text = _assistant_text_from_messages(new_messages) or streamed_assistant_text
        final_research_package = _execution_research_package(
            db_session,
            execution_id=execution.id,
            graph=runtime.graph,
            config=runtime.config,
        )
        if _execution_research_identity(runtime.graph, runtime.config) is not None:
            if final_research_package is None:
                raise ContentEvidenceInvalidError
            assistant_text = _validated_research_backed_final_answer(
                new_messages,
                research_package=final_research_package,
            )

        self.state_manager.ensure_execution_not_cancelled(
            db_session,
            execution,
            expected_worker_id=expected_worker_id,
        )
        persist_started_at = time.perf_counter()
        persisted_assistant = self.message_persister.persist_graph_messages(
            db_session,
            session_id=chat.id,
            invocation_id=invocation.id,
            execution_id=execution.id,
            messages=new_messages,
            event_writer=event_writer,
            streamed_assistant_text=streamed_assistant_text,
        )
        event_service.emit_marker(
            event_writer=event_writer,
            execution_id=execution.id,
            marker="persist_messages_ms",
            value=int((time.perf_counter() - persist_started_at) * 1000),
            messages_count=len(new_messages),
            persisted_assistant=bool(persisted_assistant),
        )

        if not persisted_assistant:
            self.state_manager.ensure_execution_not_cancelled(
                db_session,
                execution,
                expected_worker_id=expected_worker_id,
            )
            fallback_started_at = time.perf_counter()
            if not assistant_text:
                assistant_text = (
                    "The request completed, but the model did not return displayable content."
                )
            persisted_assistant = self.message_persister.persist_assistant_text(
                db_session,
                session_id=chat.id,
                invocation_id=invocation.id,
                execution_id=execution.id,
                content=assistant_text,
                event_writer=event_writer,
                emit_delta=not streamed_assistant_text,
            )
            event_service.emit_marker(
                event_writer=event_writer,
                execution_id=execution.id,
                marker="persist_assistant_fallback_ms",
                value=int((time.perf_counter() - fallback_started_at) * 1000),
            )

        self.state_manager.ensure_execution_not_cancelled(
            db_session,
            execution,
            expected_worker_id=expected_worker_id,
        )
        return ExecutionTurnResult(
            new_messages=new_messages,
            assistant_text=assistant_text,
            streamed_assistant_text=streamed_assistant_text,
            interrupt_payload=None,
            assistant_message=persisted_assistant,
        )

    def _build_graph_input(
        self,
        *,
        resume_value: Any,
        continue_from_checkpoint: bool = False,
        agent_messages: list[BaseMessage],
        system_prompt: str,
    ) -> Any:
        if continue_from_checkpoint:
            return None
        if resume_value is not None:
            return Command(resume=resume_value)
        return [
            RemoveMessage(id=REMOVE_ALL_MESSAGES),
            SystemMessage(content=system_prompt),
            *agent_messages,
        ]

    def _build_tool_api_keys(self) -> dict[str, str]:
        settings = getattr(self.container, "settings", None)
        if settings is None:
            return {}
        search_settings = getattr(settings, "search", settings)
        return {
            "traffic_relay_api_key": _secret_value(
                getattr(search_settings, "traffic_relay_api_key", ""),
            ),
            "tikhub_api_key": _secret_value(
                getattr(search_settings, "tikhub_api_key", ""),
            ),
            "metaso_api_key": _secret_value(
                getattr(search_settings, "metaso_api_key", ""),
            ),
            "anspire_api_key": _secret_value(
                getattr(search_settings, "anspire_api_key", ""),
            ),
        }

    def _stream_graph(
        self,
        *,
        db_session: Session | None,
        execution: AgentExecution,
        execution_id: str,
        graph: Any,
        graph_input: Any,
        state: dict[str, Any],
        config: dict[str, Any],
        event_writer: Any,
        event_service: AgentRuntimeEventService,
        expected_worker_id: str | None = None,
    ) -> tuple[list[BaseMessage], str, dict[str, Any] | None]:
        stream_input = (
            graph_input if isinstance(graph_input, Command) or graph_input is None else state
        )
        new_messages: list[BaseMessage] = []
        streamed_assistant_parts: list[str] = []
        streamed_assistant_by_id: dict[str, str] = {}
        stream_message_id: str = ""

        graph_started_at = time.perf_counter()
        graph_iterations = 0
        agent_iterations = 0
        current_node: str | None = None
        current_node_started_at = graph_started_at

        llm_started_at: float | None = None
        llm_message_id: str = ""
        llm_chars_in_span = 0
        llm_total_ms = 0
        llm_total_chars = 0
        llm_spans = 0

        stream_config = dict(config) if isinstance(config, dict) else {}
        status_clock = getattr(self, "_status_clock", time.monotonic)
        last_status_poll_at: float | None = None
        for item in graph.stream(
            stream_input,
            config=stream_config,
            stream_mode=["messages", "updates", "custom"],
            version="v2",
        ):
            if db_session is not None:
                status_poll_at = status_clock()
                if (
                    last_status_poll_at is None
                    or status_poll_at - last_status_poll_at >= 1.0
                ):
                    self.state_manager.ensure_execution_not_cancelled(
                        db_session,
                        execution,
                        expected_worker_id=expected_worker_id,
                    )
                    last_status_poll_at = status_poll_at
            stream_mode, payload = _stream_mode_and_payload(item)
            if stream_mode is None:
                continue

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
                            llm_total_ms += event_service.emit_llm_inference_metric(
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
                        llm_total_ms += event_service.emit_llm_inference_metric(
                            event_writer=event_writer,
                            execution_id=execution_id,
                            started_at=llm_started_at,
                            output_chars=llm_chars_in_span,
                            force=True,
                        )
                        llm_spans += 1
                        llm_total_chars += llm_chars_in_span
                    event_service.emit_marker(
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
                    agent_iterations += sum(1 for node in update_nodes if node == "agent")
                    max_iterations = int(self.container.settings.agent.max_iterations)
                    if agent_iterations > max_iterations:
                        raise RuntimeError(
                            "AGENT_MAX_ITERATIONS_EXCEEDED: "
                            f"maximum {max_iterations} model iterations"
                        )
                    next_node = update_nodes[-1]
                    if current_node is None:
                        current_node = next_node
                        current_node_started_at = now
                        event_service.emit_marker(
                            event_writer=event_writer,
                            execution_id=execution_id,
                            marker="graph_node_started",
                            node=next_node,
                            iteration=graph_iterations,
                        )
                    elif next_node != current_node:
                        event_service.emit_marker(
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
            elif stream_mode == "custom" and isinstance(payload, dict):
                public = _public_custom_event(payload)
                if public is not None:
                    event_writer.emit("agent_runtime_marker", public)

        final_time = time.perf_counter()
        if llm_started_at is not None:
            llm_total_ms += event_service.emit_llm_inference_metric(
                event_writer=event_writer,
                execution_id=execution_id,
                started_at=llm_started_at,
                output_chars=llm_chars_in_span,
                force=True,
            )
            llm_spans += 1
            llm_total_chars += llm_chars_in_span
            event_service.emit_marker(
                event_writer=event_writer,
                execution_id=execution_id,
                marker="llm_total_inference_ms",
                value=llm_total_ms,
                estimated_output_tokens=int(llm_total_chars / LLM_TOKEN_ESTIMATE_CHARS_PER_TOKEN),
                spans=llm_spans,
            )
        if current_node:
            event_service.emit_marker(
                event_writer=event_writer,
                execution_id=execution_id,
                marker="graph_node_final_ms",
                node=current_node,
                duration_ms=int((final_time - current_node_started_at) * 1000),
                iterations=graph_iterations,
            )
        event_service.emit_marker(
            event_writer=event_writer,
            execution_id=execution_id,
            marker="graph_runtime_ms",
            value=int((final_time - graph_started_at) * 1000),
            iterations=graph_iterations,
        )
        return new_messages, "".join(streamed_assistant_parts), None


class PostExecutionDispatcher(Protocol):
    def dispatch(self, execution_id: str, request_id: str | None = None) -> None: ...


class CeleryPostExecutionDispatcher:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def dispatch(self, execution_id: str, request_id: str | None = None) -> None:
        from services.tasks import process_agent_post_execution

        with Session(get_engine(self.settings)) as session:
            execution = session.get(AgentExecution, execution_id)
            if execution is None:
                return
            model_config_id = execution.model_config_id
        process_agent_post_execution.apply_async(
            kwargs={
                "execution_id": execution_id,
                "model_config_id": model_config_id,
                "request_id": request_id,
            },
            queue=self.settings.agent.celery_background_queue,
            retry=False,
        )


class AgentPostExecutionService:
    def __init__(
        self,
        *,
        container: Any,
        settings: Any,
        dispatcher: PostExecutionDispatcher | None = None,
    ) -> None:
        self.container = container
        self.settings = settings
        self.dispatcher = dispatcher

    def close(self) -> None:
        return None

    def enqueue(
        self,
        *,
        db_session: Session,
        execution: AgentExecution,
        request_id: str | None = None,
    ) -> None:
        outbox = db_session.exec(
            select(ExecutionOutbox).where(
                ExecutionOutbox.execution_id == execution.id,
                ExecutionOutbox.kind == "postprocess",
            )
        ).first()
        if outbox is None:
            outbox = ExecutionOutbox(
                execution_id=execution.id,
                model_config_id=execution.model_config_id,
                kind="postprocess",
                request_id=request_id or "",
            )
        elif outbox.status not in {"published", "completed", "failed"}:
            now = utcnow()
            outbox.status = "pending"
            outbox.available_at = now
            outbox.locked_by = None
            outbox.locked_until = None
            outbox.updated_at = now
        db_session.add(outbox)

    def schedule(
        self,
        *,
        event_service: AgentRuntimeEventService,
        event_writer: Any,
        execution: AgentExecution,
        request_id: str | None = None,
    ) -> None:
        try:
            if self.dispatcher is not None:
                self.dispatcher.dispatch(execution.id, request_id)
            event_service.emit_marker(
                event_writer=event_writer,
                execution_id=execution.id,
                marker="post_execution_dispatched",
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "Post-execution task scheduling failed for %s.",
                execution.id,
                exc_info=True,
            )
            event_service.emit_marker(
                event_writer=event_writer,
                execution_id=execution.id,
                marker="post_execution_dispatch_failed",
            )

    def process(
        self,
        *,
        execution_id: str,
        model_config_id: str,
        request_id: str | None = None,
    ) -> None:
        try:
            with Session(get_engine(self.settings)) as session:
                execution = session.get(AgentExecution, execution_id)
                if execution is None or execution.postprocess_completed_at is not None:
                    return
                if execution.model_config_id != model_config_id:
                    return
                outbox = session.exec(
                    select(ExecutionOutbox)
                    .where(
                        ExecutionOutbox.execution_id == execution_id,
                        ExecutionOutbox.kind == "postprocess",
                    )
                    .with_for_update()
                ).first()
                now = utcnow()
                if outbox is None:
                    raise RuntimeError("postprocess outbox is missing")
                if outbox.model_config_id != model_config_id:
                    return
                if (
                    outbox.status == "processing"
                    and outbox.locked_until is not None
                    and outbox.locked_until > now
                ):
                    return
                if outbox.status in {"completed", "failed"}:
                    return
                if outbox.processing_attempts >= int(
                    self.settings.agent.postprocess_max_attempts
                ):
                    outbox.status = "failed"
                    outbox.locked_by = None
                    outbox.locked_until = None
                    outbox.last_error = outbox.last_error or "Postprocess retry limit exceeded."
                    outbox.updated_at = now
                    session.add(outbox)
                    session.commit()
                    return
                outbox.status = "processing"
                outbox.processing_attempts += 1
                outbox.locked_by = "background-worker"
                outbox.locked_until = now + timedelta(
                    seconds=int(self.settings.agent.worker_soft_time_limit_seconds)
                )
                outbox.updated_at = now
                session.add(outbox)
                session.commit()

                invocation = session.get(AgentInvocation, execution.invocation_id)
                if invocation is None:
                    raise RuntimeError("postprocess invocation is missing")
                chat = session.get(ChatSession, invocation.session_id)
                user_message = session.exec(
                    select(ChatMessage)
                    .where(ChatMessage.invocation_id == invocation.id)
                    .where(ChatMessage.role == MessageRole.user)
                    .order_by(ChatMessage.created_at.asc())
                ).first()
                agent_profile = session.get(AgentProfile, invocation.agent_id)
                if chat is None or user_message is None or agent_profile is None:
                    raise RuntimeError("postprocess conversation context is incomplete")
                assistant_message = session.exec(
                    select(ChatMessage)
                    .where(ChatMessage.invocation_id == invocation.id)
                    .where(ChatMessage.role == MessageRole.assistant)
                    .order_by(ChatMessage.created_at.desc())
                ).first()
                context = {
                    "execution_id": execution.id,
                    "agent_id": chat.agent_id,
                    "user_id": invocation.user_id,
                    "session_id": chat.id,
                    "account_name": agent_profile.name,
                    "account_positioning": agent_profile.description,
                    "user_message": user_message.content,
                    "source_message_id": user_message.id,
                    "assistant_text": assistant_message.content if assistant_message else "",
                    "tool_results": [],
                    "trace_id": execution.trace_id,
                    "thread_id": chat.langgraph_thread_id,
                    "model_config_id": execution.model_config_id,
                }

            model_gateway = self.container.gateway_for_model_config(
                context["model_config_id"]
            )
            if self.settings.agent.memory_after_turn_enabled:
                _extract_memory_background(
                    **context,
                    model_gateway=model_gateway,
                    settings=self.settings,
                    request_id=request_id,
                    conversation_id=context["session_id"],
                )
            _refresh_short_summary_background(
                session_id=context["session_id"],
                settings=self.settings,
            )
            _update_title_background(
                session_id=context["session_id"],
                execution_id=context["execution_id"],
                user_id=context["user_id"],
                user_message=context["user_message"],
                settings=self.settings,
                model_gateway=model_gateway,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Post-execution processing failed: execution=%s error_type=%s",
                execution_id,
                type(exc).__name__,
            )
            self._mark_processing_failure(
                execution_id,
                classify_runtime_error(exc).message,
            )
            return

        with Session(get_engine(self.settings)) as session:
            execution = session.get(AgentExecution, execution_id)
            if execution is None:
                return
            execution.postprocess_completed_at = utcnow()
            execution.touch_updated_at()
            session.add(execution)
            outbox = session.exec(
                select(ExecutionOutbox).where(
                    ExecutionOutbox.execution_id == execution_id,
                    ExecutionOutbox.kind == "postprocess",
                )
            ).first()
            if outbox is not None:
                outbox.status = "completed"
                outbox.locked_by = None
                outbox.locked_until = None
                outbox.last_error = ""
                outbox.updated_at = utcnow()
                session.add(outbox)
            session.commit()

    def _mark_processing_failure(self, execution_id: str, error: str) -> None:
        with Session(get_engine(self.settings)) as session:
            outbox = session.exec(
                select(ExecutionOutbox)
                .where(
                    ExecutionOutbox.execution_id == execution_id,
                    ExecutionOutbox.kind == "postprocess",
                )
                .with_for_update()
            ).first()
            if outbox is None or outbox.status in {"completed", "failed"}:
                return
            now = utcnow()
            max_attempts = int(self.settings.agent.postprocess_max_attempts)
            if outbox.processing_attempts >= max_attempts:
                outbox.status = "failed"
            else:
                outbox.status = "pending"
                delay = int(self.settings.agent.postprocess_retry_base_seconds) * (
                    2 ** max(0, outbox.processing_attempts - 1)
                )
                outbox.available_at = now + timedelta(seconds=delay)
            outbox.locked_by = None
            outbox.locked_until = None
            outbox.last_error = error[:2000]
            outbox.updated_at = now
            session.add(outbox)
            session.commit()


def _allowed_hotspot_sources(agent_version: AgentVersion) -> list[str]:
    if agent_version.hotspot_sources:
        selected, unsupported = partition_hotspot_sources(agent_version.hotspot_sources)
        if unsupported:
            logger.warning(
                "Ignoring unsupported hotspot sources for agent version %s: %s",
                agent_version.id,
                ", ".join(unsupported),
            )
        if selected:
            return selected
    return list(DEFAULT_HOTSPOT_SOURCES)


def _confirmation_tool_names(registrations: Any) -> list[str]:
    return [
        registration.name
        for registration in registrations
        if registration.confirmation_policy.value == "always"
        or (
            registration.confirmation_policy.value == "configured"
            and registration.side_effecting
        )
    ]


def _secret_value(value: Any) -> str:
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        try:
            return str(getter() or "").strip()
        except Exception:
            return ""
    return str(value or "").strip()


def _extract_memory_background(
    *,
    execution_id: str,
    agent_id: str,
    user_id: str,
    session_id: str,
    account_name: str,
    account_positioning: str,
    user_message: str,
    source_message_id: str,
    assistant_text: str,
    tool_results: list[str],
    thread_id: str | None = None,
    request_id: str | None = None,
    conversation_id: str | None = None,
    model_gateway: Any,
    settings: Any,
    trace_id: str | None = None,
    model_config_id: str | None = None,
) -> None:
    _ = model_config_id
    started_at = time.perf_counter()
    with Session(get_engine(settings)) as session:
        writer = PersistentAgentEventWriter(
            execution_id,
            session.get_bind(),
            settings=settings,
            trace_id=trace_id,
            thread_id=thread_id,
            request_id=request_id,
            conversation_id=conversation_id,
        )
        try:
            repository = MemoryRepository(session)
            long_term = LongTermMemory(repository)
            extracted = long_term.remember_after_turn(
                agent_id=agent_id,
                user_id=user_id,
                session_id=session_id,
                account_name=account_name,
                account_positioning=account_positioning,
                user_message=user_message,
                source_message_id=source_message_id,
                source_execution_id=execution_id,
                assistant_response=assistant_text,
                tool_results=tool_results,
                model_gateway=model_gateway,
                callbacks=[
                    ModelUsageCallback(
                        settings,
                        UsageContext(
                            user_id=user_id,
                            session_id=session_id,
                            execution_id=execution_id,
                            category="memory_extraction",
                        ),
                    )
                ],
            )
            _emit_postprocess_marker(
                writer,
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
                    "scope": {
                        "user_id": user_id,
                        "agent_id": agent_id,
                        "session_id": session_id,
                    },
                    "memories": [
                        {
                            "key": item.key,
                            "kind": item.kind,
                            "content": item.content[:500],
                        }
                        for item in extracted
                    ],
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Background memory extraction failed: execution=%s error_type=%s",
                execution_id,
                type(exc).__name__,
            )
            _emit_postprocess_marker(
                writer,
                execution_id=execution_id,
                marker="memory_extraction_ms",
                value=int((time.perf_counter() - started_at) * 1000),
                failed=True,
            )
            writer.emit(
                "memory_extraction_failed",
                {
                    "execution_id": execution_id,
                    "error": classify_runtime_error(exc).message,
                },
            )
            raise
        finally:
            writer.close()


def _refresh_short_summary_background(*, session_id: str, settings: Any) -> None:
    with Session(get_engine(settings)) as session:
        chat = session.get(ChatSession, session_id)
        if chat is None:
            return
        repository = MemoryRepository(session)
        ShortTermMemory(repository).refresh_summary(
            session,
            session_id=session_id,
            user_id=chat.user_id,
        )


def _update_title_background(
    *,
    session_id: str,
    execution_id: str,
    user_id: str,
    user_message: str,
    settings: Any,
    model_gateway: Any | None = None,
) -> None:
    with Session(get_engine(settings)) as session:
        chat = session.get(ChatSession, session_id)
        if chat is None:
            return
        if chat.title != "New Session":
            return
        chat.title = _generate_session_title(
            user_message=user_message,
            model_gateway=model_gateway,
            callbacks=[
                ModelUsageCallback(
                    settings,
                    UsageContext(
                        user_id=user_id,
                        session_id=session_id,
                        execution_id=execution_id,
                        category="session_title",
                    ),
                )
            ],
        )
        chat.touch_updated_at()
        session.add(chat)
        session.commit()


def _generate_session_title(
    *,
    user_message: str,
    model_gateway: Any | None,
    callbacks: list[Any] | None = None,
) -> str:
    fallback = _fallback_session_title(user_message)
    if model_gateway is None:
        return fallback
    prompt = (
        "Generate a concise chat title for this user message.\n"
        "Return at most 15 characters without markdown.\n"
        f"Message:\n{user_message[:1200]}"
    )
    try:
        model = model_gateway.build_structured_output_model(SessionTitleResult)
        try:
            result = model.invoke(prompt, config={"callbacks": callbacks or []})
        except TypeError as exc:
            if "config" not in str(exc):
                raise
            result = model.invoke(prompt)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Session title generation failed: error_type=%s", type(exc).__name__)
        raise RuntimeError("Session title generation failed.") from None
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


def _emit_postprocess_marker(
    writer: Any,
    *,
    execution_id: str,
    marker: str,
    **payload: Any,
) -> None:
    writer.emit(
        "agent_runtime_marker",
        {"execution_id": execution_id, "marker": marker, **payload},
    )


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

    message_id = str(getattr(message, "id", "") or "")
    tool_chunks = getattr(message, "tool_call_chunks", None)
    if tool_chunks:
        for tool_chunk in tool_chunks:
            if not isinstance(tool_chunk, dict):
                continue
            event_writer.emit(
                "tool_progress",
                {
                    "execution_id": execution_id,
                    "message_id": message_id,
                    "tool_call_id": str(tool_chunk.get("id") or tool_chunk.get("index") or ""),
                    "tool_name": str(tool_chunk.get("name") or ""),
                    "chunk": _make_public_tool_chunk(tool_chunk),
                },
            )
        return message_id, ""
    if getattr(message, "tool_calls", None):
        return message_id, ""

    chunk_text = _message_chunk_text(message)
    if not chunk_text:
        return getattr(message, "id", "") or "", ""

    message_id = message_id or f"lc-{execution_id}-assistant"
    normalized = streamed_assistant_by_id.get(message_id, "")
    delta = _message_text_delta(previous=normalized, current=chunk_text)
    if not delta:
        return message_id, ""

    streamed_assistant_by_id[message_id] = normalized + delta
    streamed_assistant_parts.append(delta)
    event_writer.emit(
        "assistant_message_delta",
        {
            "execution_id": execution_id,
            "message_type": MessageType.markdown,
            "message_id": message_id,
            "chunk": delta,
            "done": False,
        },
    )
    return message_id, delta


def _stream_mode_and_payload(item: Any) -> tuple[str | None, Any]:
    if isinstance(item, dict):
        mode = item.get("type")
        return (str(mode), item.get("data")) if isinstance(mode, str) else (None, None)
    if isinstance(item, tuple) and len(item) == 2:
        return str(item[0]), item[1]
    return None, None


def _make_public_tool_chunk(value: dict[str, Any]) -> dict[str, Any]:
    allowed = {"name", "args", "id", "index"}
    return {key: value[key] for key in allowed if key in value}


def _public_custom_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    if payload.get("kind") != "progress":
        return None
    name = str(payload.get("name") or "agent_progress")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    public_data = {
        key: value
        for key, value in data.items()
        if key not in {"configurable", "runtime", "context"}
    }
    return {"marker": name, "progress": public_data}


def _extract_message_payload(payload: Any) -> tuple[BaseMessage | None, dict[str, Any]]:
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


def _validated_research_backed_final_answer(
    messages: list[BaseMessage],
    *,
    research_package: Any,
) -> str:
    evidence = build_supported_research_evidence(research_package)
    expected_identity = parse_research_identity(
        evidence.get("research_pack_id"), evidence.get("topic_hash")
    )
    if expected_identity is None:
        raise ContentEvidenceInvalidError
    boundary_index = _current_research_tool_boundary(messages, expected_identity=expected_identity)
    assistants = [
        message for message in messages[boundary_index + 1 :] if isinstance(message, AIMessage)
    ]
    if len(assistants) != 1:
        raise ContentEvidenceInvalidError
    final_message = assistants[0]
    if getattr(final_message, "tool_calls", None):
        raise ContentEvidenceInvalidError
    additional_kwargs = getattr(final_message, "additional_kwargs", {})
    if not isinstance(additional_kwargs, dict) or set(additional_kwargs) != {
        "research_backed_final_proof"
    }:
        raise ContentEvidenceInvalidError
    expected_answer = validate_research_final_proof(
        additional_kwargs["research_backed_final_proof"],
        evidence=evidence,
    )
    if not isinstance(final_message.content, str) or final_message.content != expected_answer:
        raise ContentEvidenceInvalidError
    return expected_answer


def _current_research_tool_boundary(
    messages: list[BaseMessage],
    *,
    expected_identity: tuple[str, str],
) -> int:
    """Return the latest current-turn research tool boundary, never a historical one."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, ToolMessage) or message.name != "prepare_topic_research":
            continue
        content = message.content
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError as exc:
                raise ContentEvidenceInvalidError from exc
        if not isinstance(content, dict):
            raise ContentEvidenceInvalidError
        tool_identity = parse_research_identity(
            content.get("research_pack_id"), content.get("research_topic_hash")
        )
        evidence = content.get("supported_evidence")
        if not isinstance(evidence, dict):
            raise ContentEvidenceInvalidError
        evidence_identity = parse_research_identity(
            evidence.get("research_pack_id"), evidence.get("topic_hash")
        )
        if tool_identity != expected_identity or evidence_identity != expected_identity:
            raise ContentEvidenceInvalidError
        return index
    raise ContentEvidenceInvalidError


def _recent_conversation_context(messages: list[BaseMessage], limit: int = 8) -> str:
    """Render enough of the persisted dialogue for intent resolution, not routing rules."""
    rows: list[str] = []
    for message in messages[-max(limit, 1) :]:
        content = message_to_text(message).strip()
        if not content:
            continue
        role = getattr(message, "type", None) or message.__class__.__name__
        rows.append(f"{role}: {content[:1200]}")
    return "\n".join(rows)


def _extract_node_names(payload: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for key, value in payload.items():
        if not isinstance(key, str) or key == "__interrupt__":
            continue
        if not isinstance(value, dict):
            continue
        names.append(key)
    return names


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


def _message_text_delta(*, previous: str, current: str) -> str:
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


def _utc_elapsed_ms(started_at: datetime | None, ended_at: datetime | None) -> int:
    if started_at is None or ended_at is None:
        return 0
    try:
        return max(int((ended_at - started_at).total_seconds() * 1000), 0)
    except Exception:
        return 0
