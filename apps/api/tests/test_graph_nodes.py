import json

import agent.graph.nodes as graph_nodes
import httpx
import pytest
from agent.graph.factory import AgentGraphBuilder
from agent.graph.nodes import ToolIntentDecision, build_agent_node, build_tool_error_node
from agent.workflows.deep_research import ContentEvidenceInvalidError
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool


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


class _TextualToolCallModel:
    def invoke(self, _messages: object) -> AIMessage:
        return AIMessage(
            content=(
                "我来帮你抓取当前热点。\n\n"
                '<invoke name="fetch_hotspots">\n'
                '<parameter name="query">商业 消费 品牌 平台</parameter>\n'
                "</invoke>\n"
                '{"result":"这是模型伪造的热点结果，不得作为最终回复。"}'
            )
        )


class _SystemWarningToolCallModel:
    def invoke(self, _messages: object) -> AIMessage:
        return AIMessage(
            content=(
                "我来帮你获取实时热点。\n\n"
                "<system_warning>調用開始 fetch_hotspots 中斷聊天 "
                "hjmedia_pipeline 上 (ID: toolu_016CxTQAj2qBBP8mA7ekY23s)</system_warning>\n\n"
                "<function_results>Error executing tool: rate limited</function_results>\n\n"
                "稍等片刻后我再帮你重试。"
            )
        )


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


def test_agent_node_normalizes_provider_textual_tool_call_before_routing():
    @tool("fetch_hotspots")
    def fetch_hotspots(
        source: str = "all",
        platforms: str = "all",
        rss_sources: str = "all",
    ) -> dict[str, str]:
        """Fetch current hotspots."""
        return {"result": "real tool result"}

    result = build_agent_node(
        _TextualToolCallModel(),
        tools=[fetch_hotspots],
    )(
        {
            "messages": [HumanMessage(content="继续执行刚才的动作")],
            "available_tool_names": ["fetch_hotspots"],
            "task_status": "thinking",
        }
    )

    message = result["messages"][0]
    assert message.content == ""
    assert message.tool_calls == [
        {
            "name": "fetch_hotspots",
            "args": {},
            "id": message.tool_calls[0]["id"],
            "type": "tool_call",
        }
    ]
    assert result["task_status"] == "executing"


def test_agent_node_normalizes_provider_system_warning_tool_call_before_routing():
    @tool("fetch_hotspots")
    def fetch_hotspots(
        source: str = "all",
        platforms: str = "all",
        rss_sources: str = "all",
    ) -> dict[str, str]:
        """Fetch current hotspots."""
        return {"result": "real tool result"}

    result = build_agent_node(
        _SystemWarningToolCallModel(),
        tools=[fetch_hotspots],
    )(
        {
            "messages": [HumanMessage(content="继续执行刚才的动作")],
            "available_tool_names": ["fetch_hotspots"],
            "task_status": "thinking",
        }
    )

    message = result["messages"][0]
    assert message.content == ""
    assert message.tool_calls[0]["name"] == "fetch_hotspots"
    assert message.tool_calls[0]["args"] == {}
    assert result["task_status"] == "executing"


def test_agent_node_does_not_repeat_hotspot_fetch_after_tool_result():
    @tool("fetch_hotspots")
    def fetch_hotspots() -> dict[str, str]:
        """Fetch current hotspots."""
        return {"result": "real tool result"}

    result = build_agent_node(
        _Model(),
        tools=[fetch_hotspots],
    )(
        {
            "messages": [
                HumanMessage(content="找热点"),
                ToolMessage(
                    content=json.dumps(
                        {
                            "result": "real tool result",
                            "filtering": {"result_ready": True},
                        }
                    ),
                    name="fetch_hotspots",
                    tool_call_id="call-hotspots",
                ),
            ],
            "available_tool_names": ["fetch_hotspots"],
            "task_status": "thinking",
        }
    )

    message = result["messages"][0]
    assert message.content == "real tool result"
    assert not message.tool_calls
    assert result["task_status"] == "completed"


def test_agent_node_uses_model_intent_gate_when_native_tool_call_is_missing():
    @tool("prepare_topic_research")
    def prepare_topic_research(topic: str) -> dict[str, str]:
        """Research one confirmed topic."""
        return {"topic": topic}

    class _NarratingModel:
        def invoke(self, _messages: object) -> AIMessage:
            return AIMessage(
                content="Let me call the research tool. Calling prepare_topic_research now."
            )

    class _IntentModel:
        def __init__(self) -> None:
            self.messages: list[object] = []

        def invoke(self, messages: list[object]) -> ToolIntentDecision:
            self.messages = messages
            return ToolIntentDecision(
                action="call_tool",
                tool_name="prepare_topic_research",
                arguments={"topic": "携程51.79亿罚单完整解读,垄断生意走到头"},
            )

    intent_model = _IntentModel()

    result = build_agent_node(
        _NarratingModel(),
        tools=[prepare_topic_research],
        tool_intent_model=intent_model,
    )(
        {
            "messages": [
                AIMessage(content="1. **携程罚单完整解读** — 82 分"),
                HumanMessage(content="换个角度分析携程罚单背后的平台规则"),
            ],
            "available_tool_names": ["prepare_topic_research"],
            "task_status": "thinking",
        }
    )

    message = result["messages"][0]
    assert message.content == ""
    assert message.tool_calls[0]["name"] == "prepare_topic_research"
    assert message.tool_calls[0]["args"] == {
        "topic": "携程51.79亿罚单完整解读,垄断生意走到头"
    }
    assert any(
        "Candidate assistant reply" in str(message.content)
        for message in intent_model.messages
    )


def test_agent_node_preserves_native_tool_calls_without_intent_gate():
    @tool("fetch_hotspots")
    def fetch_hotspots() -> dict[str, str]:
        """Fetch current hotspots."""
        return {"result": "real tool result"}

    class _NativeToolCallModel:
        def invoke(self, _messages: object) -> AIMessage:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "fetch_hotspots",
                        "args": {},
                        "id": "call-native-hotspots",
                        "type": "tool_call",
                    }
                ],
            )

    class _FailingIntentModel:
        def invoke(self, _messages: object) -> ToolIntentDecision:
            raise AssertionError("native calls must bypass the intent gate")

    result = build_agent_node(
        _NativeToolCallModel(),
        tools=[fetch_hotspots],
        tool_intent_model=_FailingIntentModel(),
    )(
        {
            "messages": [HumanMessage(content="给我看今天热点")],
            "available_tool_names": ["fetch_hotspots"],
            "task_status": "thinking",
        }
    )

    message = result["messages"][0]
    assert message.tool_calls[0]["name"] == "fetch_hotspots"
    assert result["task_status"] == "executing"


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


def test_research_final_node_formats_selected_evidence_with_isolated_callbacks():
    final_model = _SelectionModel([{"claim_ids": ["clm_node_claim"]}])
    presentation_model = _SelectionModel(
        [
            {
                "content": (
                    "## 核心结论\n\n这是面向用户的研究简报，内容只来自已核验资料。\n\n"
                    "## 来源\n\n[节点来源](https://node.example/source)"
                )
            }
        ]
    )

    result = build_agent_node(
        _Model(),
        research_final_model=final_model,
        research_presentation_model=presentation_model,
    )(_research_final_state())

    message = result["messages"][0]
    assert message.content.startswith("## 核心结论")
    assert "S1" not in message.content
    assert set(message.additional_kwargs) == {"research_backed_final_proof"}
    assert message.additional_kwargs["research_backed_final_proof"]["claim_ids"] == [
        "clm_node_claim"
    ]
    assert final_model.calls[0][1] == {"callbacks": []}
    assert presentation_model.calls[0][1] == {"callbacks": []}


def test_research_final_model_factory_is_not_built_for_plain_chat():
    calls: list[str] = []

    def build_final_model() -> _SelectionModel:
        calls.append("built")
        return _SelectionModel([{"claim_ids": ["clm_node_claim"]}])

    def build_presentation_model() -> _SelectionModel:
        calls.append("presentation-built")
        return _SelectionModel(
            [
                {
                    "content": (
                        "## 核心结论\n\n这是仅依据已核验资料生成的说明。\n\n"
                        "## 来源\n\n[节点来源](https://node.example/source)"
                    )
                }
            ]
        )

    plain_result = build_agent_node(
        _Model(),
        research_final_model=build_final_model,
        research_presentation_model=build_presentation_model,
    )(
        {"messages": [], "task_status": "thinking"}
    )
    research_result = build_agent_node(
        _Model(),
        research_final_model=build_final_model,
        research_presentation_model=build_presentation_model,
    )(
        _research_final_state()
    )

    assert plain_result["messages"][0].content == "Hello world"
    assert research_result["messages"][0].content.startswith("## 核心结论")
    assert calls == ["built", "presentation-built"]


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
