from agent.graph.nodes import build_agent_node
from agent.runtime.events import event_writer_scope
from langchain_core.messages import AIMessageChunk


class _EventWriter:
    execution_id = "exe-node"

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, data: dict) -> None:
        self.events.append((event, data))


def test_agent_node_does_not_emit_assistant_delta_directly():
    class _Model:
        def stream(self, _messages: object):
            yield AIMessageChunk(content="Hello")
            yield AIMessageChunk(content=" world")

    writer = _EventWriter()
    node = build_agent_node(_Model())

    with event_writer_scope(writer):
        result = node({"messages": [], "iterations": 0})

    assert result["messages"][0].content == "Hello world"
    assert [name for name, _ in writer.events] == ["agent_node", "agent_node"]
