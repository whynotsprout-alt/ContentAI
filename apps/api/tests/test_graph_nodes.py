import agent.graph.nodes as graph_nodes
import httpx
import pytest
from agent.graph.factory import AgentGraphBuilder
from agent.graph.nodes import build_agent_node, build_tool_error_node
from langchain_core.messages import AIMessage, ToolMessage


class _EventWriter:
    execution_id = "exe-node"

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, data: dict) -> None:
        self.events.append((event, data))


class _Model:
    def invoke(self, _messages: object) -> AIMessage:
        return AIMessage(content="Hello world")


class _FlakyStreamModel:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def invoke(self, _messages: object) -> AIMessage:
        self.calls += 1
        if self.calls <= self.failures:
            raise httpx.RemoteProtocolError("incomplete chunked read")
        return AIMessage(content="Recovered")


def test_agent_node_does_not_emit_assistant_delta_directly():
    writer = _EventWriter()
    node = build_agent_node(_Model())

    result = node(
        {"messages": [], "task_status": "thinking"},
        config={"metadata": {"event_writer": writer}},
    )

    assert result["messages"][0].content == "Hello world"
    assert [name for name, _ in writer.events] == ["agent_node", "agent_node"]


def test_agent_node_retries_disconnected_model_stream_before_returning_result(
    monkeypatch: pytest.MonkeyPatch,
):
    model = _FlakyStreamModel(failures=2)
    monkeypatch.setattr(graph_nodes, "sleep", lambda _seconds: None)

    result = build_agent_node(model)({"messages": [], "task_status": "thinking"})

    assert result["messages"][0].content == "Recovered"
    assert model.calls == 3


def test_agent_node_stops_after_limited_stream_retries(monkeypatch: pytest.MonkeyPatch):
    model = _FlakyStreamModel(failures=3)
    monkeypatch.setattr(graph_nodes, "sleep", lambda _seconds: None)

    with pytest.raises(httpx.RemoteProtocolError):
        build_agent_node(model)({"messages": [], "task_status": "thinking"})

    assert model.calls == graph_nodes.MODEL_STREAM_MAX_ATTEMPTS


def test_tool_error_node_does_not_append_an_assistant_prefill_message():
    result = build_tool_error_node()(
        {
            "messages": [
                ToolMessage(
                    content={"status": "failed", "error": "source timed out"},
                    name="fetch_hotspots",
                    tool_call_id="call-1",
                    status="error",
                )
            ],
            "tool_error": "source timed out",
            "tool_error_count": 1,
            "task_status": "error",
        }
    )

    assert result["messages"] == []
    assert result["tool_error"] is None
    assert result["task_status"] == "thinking"


def test_graph_builder_accepts_invokable_model_contract():
    AgentGraphBuilder(model=_Model(), tools=[])._validate()

    with pytest.raises(TypeError):
        AgentGraphBuilder(model=object(), tools=[])._validate()


def test_graph_nodes_no_longer_embed_memory_side_effects():
    assert not hasattr(graph_nodes, "build_memory_node")
