from __future__ import annotations

from types import SimpleNamespace

import pytest
from agent.runtime.errors import classify_runtime_error
from agent.runtime.execution_services import AgentExecutionEngine, AgentRuntimeEventService
from agent.workflows.deep_research import ContentEvidenceInvalidError
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)


class _EventWriter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, data: dict) -> None:
        self.events.append((event, data))


def _make_engine() -> AgentExecutionEngine:
    engine = AgentExecutionEngine.__new__(AgentExecutionEngine)
    engine.container = SimpleNamespace(
        settings=SimpleNamespace(agent=SimpleNamespace(max_iterations=24))
    )
    return engine


def _run_stream_graph(
    *,
    graph: object,
    event_writer: _EventWriter,
    execution_id: str,
):
    engine = _make_engine()
    return engine._stream_graph(
        graph=graph,
        db_session=None,
        execution=SimpleNamespace(id=execution_id),
        execution_id=execution_id,
        graph_input=[],
        state={},
        config={},
        event_writer=event_writer,
        event_service=AgentRuntimeEventService(),
    )


def _find_marker(events: list[tuple[str, dict]], marker: str) -> list[dict]:
    return [
        payload
        for event_name, payload in events
        if event_name == "agent_runtime_marker" and payload.get("marker") == marker
    ]


def test_execution_failed_event_carries_retryable_marker():
    writer = _EventWriter()
    execution = SimpleNamespace(id="exe-retryable", current_attempt_id="att-retryable")

    AgentRuntimeEventService().emit_execution_failed(
        writer,
        execution,
        "模型服务的流式连接意外中断，请稍后重试。",
        error_code="MODEL_STREAM_INTERRUPTED",
        retryable=True,
    )

    assert writer.events[-1] == (
        "run_error",
        {
            "execution_id": "exe-retryable",
            "error": "模型服务的流式连接意外中断，请稍后重试。",
            "error_code": "MODEL_STREAM_INTERRUPTED",
            "retryable": True,
        },
    )


def test_content_evidence_error_keeps_public_terminal_code():
    detail = classify_runtime_error(ContentEvidenceInvalidError())

    assert detail.code == "CONTENT_EVIDENCE_INVALID"
    assert detail.message == "Research evidence could not be validated."
    assert detail.retryable is False


def test_build_graph_input_keeps_system_prompt_for_fresh_thread():
    engine = AgentExecutionEngine.__new__(AgentExecutionEngine)

    graph_input = engine._build_graph_input(
        resume_value=None,
        agent_messages=[HumanMessage(content="Runtime context:"), HumanMessage(content="hello")],
        system_prompt="System prompt",
    )

    assert isinstance(graph_input[0], RemoveMessage)
    assert isinstance(graph_input[1], SystemMessage)
    assert graph_input[1].content == "System prompt"
    assert [message.content for message in graph_input[2:]] == ["Runtime context:", "hello"]


def test_build_graph_input_does_not_append_system_prompt_to_checkpoint_thread():
    engine = AgentExecutionEngine.__new__(AgentExecutionEngine)

    graph_input = engine._build_graph_input(
        resume_value=None,
        agent_messages=[
            HumanMessage(content="Runtime context:"),
            HumanMessage(content="previous request"),
            AIMessage(content="previous response"),
            HumanMessage(content="next request"),
        ],
        system_prompt="Updated system prompt",
    )

    assert isinstance(graph_input[0], RemoveMessage)
    assert isinstance(graph_input[1], SystemMessage)
    assert graph_input[1].content == "Updated system prompt"
    assert [message.content for message in graph_input[2:]] == [
        "Runtime context:",
        "previous request",
        "previous response",
        "next request",
    ]


def test_stream_graph_emits_assistant_delta_events():
    class _StreamingGraph:
        def stream(self, *_args: object, **_kwargs: object):
            yield ("messages", (AIMessageChunk(content="Hello "), {"langgraph_node": "agent"}))
            yield ("messages", (AIMessageChunk(content="world"), {"langgraph_node": "agent"}))
            yield ("updates", {"agent": {"messages": [AIMessage(content="Hello world")]}})

    writer = _EventWriter()
    messages, streamed_text, interrupt = _run_stream_graph(
        graph=_StreamingGraph(),
        execution_id="exe-stream-delta",
        event_writer=writer,
    )

    assert interrupt is None
    assert streamed_text == "Hello world"
    assert len(messages) == 1
    assert messages[0].content == "Hello world"  # type: ignore[union-attr]
    delta_payloads = [
        payload for event_name, payload in writer.events if event_name == "assistant_message_delta"
    ]
    assert len(delta_payloads) >= 2
    assert all(isinstance(payload.get("chunk"), str) for payload in delta_payloads)


def test_stream_graph_stream_failure_is_not_fallbacked():
    class _NoStreamingGraph:
        def stream(self, *_args: object, **_kwargs: object):
            raise RuntimeError("streaming unsupported")

    writer = _EventWriter()
    with pytest.raises(RuntimeError, match="streaming unsupported"):
        _run_stream_graph(
            graph=_NoStreamingGraph(),
            execution_id="exe-stream-fallback",
            event_writer=writer,
        )
    assert [name for name, _ in writer.events if name == "graph_invoke_fallback_ms"] == []
    assert [name for name, _ in writer.events if name == "streaming_fallback"] == []


def test_stream_graph_records_graph_node_markers():
    class _SwitchingGraph:
        def stream(self, *_args: object, **_kwargs: object):
            yield ("updates", {"agent": {"messages": [AIMessage(content="start")]}})
            yield ("messages", (AIMessageChunk(content="模型"), {"langgraph_node": "agent"}))
            yield ("updates", {"tools": {"messages": [AIMessage(content="工具中间结果")]}})
            yield ("messages", (AIMessageChunk(content=" output"), {"langgraph_node": "tools"}))
            yield ("updates", {"agent": {"messages": [AIMessage(content="分析完毕")]}})

    writer = _EventWriter()
    messages, streamed_text, interrupt = _run_stream_graph(
        graph=_SwitchingGraph(),
        execution_id="exe-node-markers",
        event_writer=writer,
    )

    assert interrupt is None
    assert messages
    assert isinstance(streamed_text, str)

    marker_names = {
        payload.get("marker")
        for event_name, payload in writer.events
        if event_name == "agent_runtime_marker"
    }
    assert "graph_node_started" in marker_names
    assert "graph_node_switch_ms" in marker_names
    assert "graph_node_final_ms" in marker_names
    assert "graph_runtime_ms" in marker_names
    assert _find_marker(writer.events, "llm_total_inference_ms"), (
        "LLM total inference marker should exist"
    )


def test_stream_graph_dedups_cumulative_ai_message_chunks():
    class _CumulativeStreamingGraph:
        def stream(self, *_args: object, **_kwargs: object):
            yield ("messages", (AIMessageChunk(content="This is a "), {"langgraph_node": "agent"}))
            yield (
                "messages",
                (AIMessageChunk(content="This is a test"), {"langgraph_node": "agent"}),
            )
            yield (
                "messages",
                (
                    AIMessageChunk(content="This is a test response"),
                    {"langgraph_node": "agent"},
                ),
            )
            yield (
                "updates",
                {"agent": {"messages": [AIMessage(content="This is a test response")]}},
            )

    writer = _EventWriter()
    messages, streamed_text, interrupt = _run_stream_graph(
        graph=_CumulativeStreamingGraph(),
        execution_id="exe-node-dedupe",
        event_writer=writer,
    )

    assert interrupt is None
    assert streamed_text == "This is a test response"
    delta_payloads = [
        payload.get("chunk")
        for event_name, payload in writer.events
        if event_name == "assistant_message_delta"
    ]
    assert delta_payloads == ["This is a ", "test", " response"]


def test_stream_graph_polls_cancellation_at_most_once_per_second():
    class _BusyGraph:
        def stream(self, *_args: object, **_kwargs: object):
            for index in range(20):
                yield ("updates", {"tools": {"messages": [AIMessage(content=str(index))]}})

    class _Clock:
        def __init__(self) -> None:
            self.current = 0.0

        def __call__(self) -> float:
            self.current += 0.2
            return self.current

    class _StateManager:
        def __init__(self, clock: _Clock) -> None:
            self.clock = clock
            self.polls: list[float] = []

        def ensure_execution_not_cancelled(self, *_args, **_kwargs) -> None:
            self.polls.append(self.clock.current)

    clock = _Clock()
    state_manager = _StateManager(clock)
    engine = _make_engine()
    engine._status_clock = clock
    engine.state_manager = state_manager

    engine._stream_graph(
        graph=_BusyGraph(),
        db_session=object(),
        execution=SimpleNamespace(id="execution-poll"),
        execution_id="execution-poll",
        graph_input=[],
        state={},
        config={},
        event_writer=_EventWriter(),
        event_service=AgentRuntimeEventService(),
    )

    assert 1 < len(state_manager.polls) < 20
    assert all(
        current - previous >= 1.0
        for previous, current in zip(
            state_manager.polls,
            state_manager.polls[1:],
            strict=False,
        )
    )
