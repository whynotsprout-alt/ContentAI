from __future__ import annotations

from types import SimpleNamespace

import pytest
from agent.runtime.runner import AgentRunner
from langchain_core.messages import AIMessage, AIMessageChunk


class _FakeStateManager:
    def ensure_execution_not_cancelled(self, *_args: object, **_kwargs: object) -> None:
        return None

    def heartbeat_run(self, *_args: object, **_kwargs: object) -> None:
        return None


class _EventWriter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, data: dict) -> None:
        self.events.append((event, data))


def _find_marker(events: list[tuple[str, dict]], marker: str) -> list[dict]:
    return [
        payload
        for event_name, payload in events
        if event_name == "agent_runtime_marker" and payload.get("marker") == marker
    ]


def test_stream_graph_emits_assistant_delta_events():
    class _StreamingGraph:
        def stream(self, *_args: object, **_kwargs: object):
            yield ("messages", (AIMessageChunk(content="Hello "), {"langgraph_node": "agent"}))
            yield ("messages", (AIMessageChunk(content="world"), {"langgraph_node": "agent"}))
            yield ("updates", {"agent": {"messages": [AIMessage(content="Hello world")]}})

    runner = AgentRunner(
        SimpleNamespace(
            state_manager=_FakeStateManager(),
            graph=_StreamingGraph(),
        )
    )
    writer = _EventWriter()
    messages, streamed_text, interrupt = runner._stream_graph(
        db_session=None,
        execution_id="exe-stream-delta",
        graph_input=[],
        state={},
        config={},
        event_writer=writer,
    )

    assert interrupt is None
    assert streamed_text == "Hello world"
    assert len(messages) == 1
    assert messages[0].content == "Hello world"  # type: ignore[union-attr]
    delta_payloads = [
        payload
        for event_name, payload in writer.events
        if event_name == "assistant_message_delta"
    ]
    assert len(delta_payloads) >= 2
    assert all(isinstance(payload.get("chunk"), str) for payload in delta_payloads)


def test_stream_graph_stream_failure_is_not_fallbacked():
    class _NoStreamingGraph:
        def stream(self, *_args: object, **_kwargs: object):
            raise RuntimeError("streaming unsupported")

    runner = AgentRunner(
        SimpleNamespace(
            state_manager=_FakeStateManager(),
            graph=_NoStreamingGraph(),
        )
    )
    writer = _EventWriter()
    with pytest.raises(RuntimeError, match="streaming unsupported"):
        runner._stream_graph(
            db_session=None,
            execution_id="exe-stream-fallback",
            graph_input=[],
            state={},
            config={},
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


    runner = AgentRunner(
        SimpleNamespace(
            state_manager=_FakeStateManager(),
            graph=_SwitchingGraph(),
        )
    )
    writer = _EventWriter()
    messages, streamed_text, interrupt = runner._stream_graph(
        db_session=None,
        execution_id="exe-node-markers",
        graph_input=[],
        state={},
        config={},
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


    runner = AgentRunner(
        SimpleNamespace(
            state_manager=_FakeStateManager(),
            graph=_CumulativeStreamingGraph(),
        )
    )
    writer = _EventWriter()
    messages, streamed_text, interrupt = runner._stream_graph(
        db_session=None,
        execution_id="exe-node-dedupe",
        graph_input=[],
        state={},
        config={},
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
