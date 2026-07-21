import json

import agent.graph.nodes as graph_nodes
import httpx
import pytest
from agent.graph.factory import AgentGraphBuilder
from agent.graph.nodes import build_agent_node, build_tool_error_node
from agent.workflows.deep_research import ContentEvidenceInvalidError
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


class _SelectionModel:
    def __init__(self, responses: list[object]) -> None:
        self.responses = iter(responses)
        self.calls: list[tuple[list[object], object]] = []

    def invoke(self, messages: list[object], *, config: object) -> object:
        self.calls.append((messages, config))
        return next(self.responses)


def _research_final_state() -> dict[str, object]:
    return {
        "research_package_id": "rsp-node",
        "research_topic_hash": "topic-node",
        "messages": [
            ToolMessage(
                content=json.dumps(
                    {
                        "research_pack_id": "rsp-node",
                        "research_topic_hash": "topic-node",
                        "supported_evidence": {
                            "research_pack_id": "rsp-node",
                            "topic_hash": "topic-node",
                            "topic": "node topic",
                            "claims": [
                                {
                                    "claim_id": "clm_node_claim",
                                    "kind": "finding",
                                    "claim": "Durable node finding.",
                                    "evidence": "Durable node evidence.",
                                    "source_ids": ["S1"],
                                }
                            ],
                            "sources": [
                                {
                                    "source_id": "S1",
                                    "title": "Node source",
                                    "url": "https://node.example/source",
                                    "summary": "Node summary",
                                    "publisher": "",
                                }
                            ],
                        },
                    }
                ),
                name="prepare_topic_research",
                tool_call_id="call-final-node",
            )
        ],
        "task_status": "thinking",
    }


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


def test_research_tool_result_is_carried_in_execution_graph_state():
    result = graph_nodes._research_state_update(
        {
            "messages": [
                ToolMessage(
                    content=json.dumps(
                        {
                            "research_pack_id": "rsp-local",
                            "research_topic_hash": "hash-local",
                        }
                    ),
                    name="prepare_topic_research",
                    tool_call_id="call-research",
                )
            ]
        }
    )

    assert result["research_package_id"] == "rsp-local"
    assert result["research_topic_hash"] == "hash-local"


def test_research_final_node_renders_only_deterministic_claim_selection_with_isolated_callbacks():
    final_model = _SelectionModel([{"claim_ids": ["clm_node_claim"]}])

    result = build_agent_node(_Model(), research_final_model=final_model)(_research_final_state())

    message = result["messages"][0]
    assert message.content == (
        "Research-backed findings:\n"
        "1. Durable node finding. [S1](https://node.example/source)"
    )
    assert set(message.additional_kwargs) == {"research_backed_final_proof"}
    assert message.additional_kwargs["research_backed_final_proof"]["claim_ids"] == [
        "clm_node_claim"
    ]
    assert final_model.calls[0][1] == {"callbacks": []}


def test_research_final_model_factory_is_not_built_for_plain_chat():
    calls: list[str] = []

    def build_final_model() -> _SelectionModel:
        calls.append("built")
        return _SelectionModel([{"claim_ids": ["clm_node_claim"]}])

    plain_result = build_agent_node(_Model(), research_final_model=build_final_model)(
        {"messages": [], "task_status": "thinking"}
    )
    research_result = build_agent_node(_Model(), research_final_model=build_final_model)(
        _research_final_state()
    )

    assert plain_result["messages"][0].content == "Hello world"
    assert research_result["messages"][0].content.startswith("Research-backed findings:")
    assert calls == ["built"]


@pytest.mark.parametrize(
    "content",
    [
        {"research_pack_id": "rsp-partial"},
        {"research_topic_hash": "topic-partial"},
    ],
)
def test_research_state_rejects_partial_package_identity(content):
    with pytest.raises(ContentEvidenceInvalidError):
        graph_nodes._research_state_update(
            {
                "messages": [
                    ToolMessage(
                        content=json.dumps(content),
                        name="prepare_topic_research",
                        tool_call_id="call-partial-identity",
                    )
                ]
            }
        )
