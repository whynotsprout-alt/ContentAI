import json

import contentai.agent.graph.nodes as graph_nodes
import httpx
import pytest
from contentai.agent.graph.factory import AgentGraphBuilder
from contentai.agent.graph.nodes import build_agent_node, build_tool_error_node
from contentai.agent.runtime.context import ToolRuntimeContext, tool_runtime_scope
from contentai.agent.runtime.model_invocation import InvocationAttempt
from contentai.agent.workflows.deep_research import ContentEvidenceInvalidError
from langchain_core.callbacks import CallbackManager
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


class _EventWriter:
    execution_id = "exe-node"

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event: str, data: dict) -> None:
        self.events.append((event, data))


class _Model:
    def invoke(self, _messages: object) -> AIMessage:
        return AIMessage(content="Hello world")


class _CallbackModel:
    def __init__(self) -> None:
        self.configs: list[object] = []

    def invoke(self, _messages: object, *, config: object) -> AIMessage:
        self.configs.append(config)
        return AIMessage(content="Callback-aware response")


class _FlakyStreamModel:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def invoke(self, _messages: object, *, config: object) -> AIMessage:
        _ = config
        self.calls += 1
        if self.calls <= self.failures:
            raise httpx.RemoteProtocolError("incomplete chunked read")
        return AIMessage(content="Recovered")


def _model_http_status_error(
    status_code: int,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://models.example.test/v1/chat/completions")
    response = httpx.Response(status_code, request=request, headers=headers)
    return httpx.HTTPStatusError(
        f"provider returned HTTP {status_code}",
        request=request,
        response=response,
    )


class _FlakyHttpStatusModel:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.calls = 0

    def invoke(self, _messages: object, *, config: object) -> AIMessage:
        _ = config
        self.calls += 1
        if self.calls == 1:
            raise _model_http_status_error(
                self.status_code,
                headers={"Retry-After": "2"},
            )
        return AIMessage(content="Recovered from HTTP status")


class _PartialFailureModel:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, _messages: object, *, config: dict[str, object]) -> AIMessage:
        self.calls += 1
        for callback in config["callbacks"]:
            callback.on_llm_new_token(token="partial")
        raise httpx.RemoteProtocolError("incomplete chunked read")


class _PartialHttpStatusFailureModel:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, _messages: object, *, config: dict[str, object]) -> AIMessage:
        self.calls += 1
        for callback in config["callbacks"]:
            callback.on_llm_new_token(token="partial")
        raise _model_http_status_error(429)


class _UnobservableLegacyFailureModel:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, _messages: object) -> AIMessage:
        self.calls += 1
        raise httpx.RemoteProtocolError("output may already have escaped")


class _TimeoutAwareFlakyModel:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def invoke(
        self,
        _messages: object,
        *,
        config: dict[str, object],
        **kwargs: object,
    ) -> AIMessage:
        _ = config
        self.timeouts.append(float(kwargs["timeout"]))
        if len(self.timeouts) == 1:
            raise httpx.ConnectError("connection refused")
        return AIMessage(content="Recovered within deadline")


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


def test_agent_node_propagates_runtime_callbacks_to_model():
    callback = object()
    model = _CallbackModel()

    result = build_agent_node(model)(
        {"messages": [], "task_status": "thinking"},
        config={"callbacks": [callback]},
    )

    assert result["messages"][0].content == "Callback-aware response"
    received_callbacks = model.configs[0]["callbacks"]
    assert received_callbacks[0] is callback
    assert len(received_callbacks) == 2


def test_agent_node_retries_disconnected_model_stream_before_returning_result(
    monkeypatch: pytest.MonkeyPatch,
):
    model = _FlakyStreamModel(failures=2)
    monkeypatch.setattr(graph_nodes, "sleep", lambda _seconds: None)

    result = build_agent_node(model)({"messages": [], "task_status": "thinking"})

    assert result["messages"][0].content == "Recovered"
    assert model.calls == 3


@pytest.mark.parametrize("status_code", [429, 503])
def test_agent_node_retries_transient_http_status_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    model = _FlakyHttpStatusModel(status_code)
    sleeps: list[float] = []
    monkeypatch.setattr("contentai.agent.runtime.errors.random.random", lambda: 0.0)
    monkeypatch.setattr(graph_nodes, "sleep", sleeps.append)

    result = build_agent_node(model)({"messages": [], "task_status": "thinking"})

    assert result["messages"][0].content == "Recovered from HTTP status"
    assert model.calls == 2
    assert sleeps == [2.0]


def test_agent_node_stops_after_limited_stream_retries(monkeypatch: pytest.MonkeyPatch):
    model = _FlakyStreamModel(failures=3)
    monkeypatch.setattr(graph_nodes, "sleep", lambda _seconds: None)

    with pytest.raises(httpx.RemoteProtocolError):
        build_agent_node(model)({"messages": [], "task_status": "thinking"})

    assert model.calls == graph_nodes.MODEL_STREAM_MAX_ATTEMPTS


def test_agent_node_does_not_replay_after_partial_model_output():
    model = _PartialFailureModel()

    with pytest.raises(httpx.RemoteProtocolError):
        build_agent_node(model)({"messages": [], "task_status": "thinking"})

    assert model.calls == 1


def test_agent_node_does_not_replay_http_status_after_partial_model_output() -> None:
    model = _PartialHttpStatusFailureModel()

    with pytest.raises(httpx.HTTPStatusError):
        build_agent_node(model)({"messages": [], "task_status": "thinking"})

    assert model.calls == 1


def test_agent_node_does_not_replay_when_legacy_model_output_is_unobservable():
    model = _UnobservableLegacyFailureModel()

    with pytest.raises(httpx.RemoteProtocolError):
        build_agent_node(model)({"messages": [], "task_status": "thinking"})

    assert model.calls == 1


def test_agent_node_retries_with_only_the_shared_deadline_remaining(monkeypatch):
    model = _TimeoutAwareFlakyModel()
    clock = iter([0.0, 0.0, 100.0, 100.0])
    monkeypatch.setattr(graph_nodes, "monotonic", lambda: next(clock))
    monkeypatch.setattr(graph_nodes, "sleep", lambda _seconds: None)

    result = build_agent_node(model)({"messages": [], "task_status": "thinking"})

    assert result["messages"][0].content == "Recovered within deadline"
    assert model.timeouts == [240.0, 200.0]


def test_invocation_attempt_tracks_langchain_keyword_token_dispatch():
    attempt = InvocationAttempt()
    run_manager = CallbackManager([attempt]).on_chat_model_start(
        {},
        [[HumanMessage(content="ping")]],
    )[0]

    run_manager.on_llm_new_token("partial")

    assert attempt.emitted_tokens is True


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
        "## 选题研究摘要\n\n"
        "### 关键事实\n"
        "1. Durable node finding.\n"
        "   Durable node evidence.\n"
        "   - 参考资料：[Node source](https://node.example/source)"
    )
    assert set(message.additional_kwargs) == {"research_backed_final_proof"}
    assert message.additional_kwargs["research_backed_final_proof"]["claim_ids"] == [
        "clm_node_claim"
    ]
    assert final_model.calls[0][1] == {"callbacks": []}


def test_research_final_repair_uses_only_the_shared_deadline_remaining(monkeypatch):
    class TimeoutSelectionModel:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def invoke(
            self,
            _messages: list[object],
            *,
            config: object,
            **kwargs: object,
        ) -> object:
            _ = config
            self.timeouts.append(float(kwargs["timeout"]))
            if len(self.timeouts) == 1:
                return {"claim_ids": ["unknown-claim"]}
            return {"claim_ids": ["clm_node_claim"]}

    final_model = TimeoutSelectionModel()
    clock = iter([0.0, 0.0, 200.0])
    monkeypatch.setattr(graph_nodes, "monotonic", lambda: next(clock))

    result = build_agent_node(_Model(), research_final_model=final_model)(
        _research_final_state()
    )

    assert result["messages"][0].content.startswith("## 选题研究摘要")
    assert final_model.timeouts == [240.0, 100.0]


def test_research_final_node_uses_only_the_isolated_research_final_callback():
    callback = object()
    final_model = _SelectionModel([{"claim_ids": ["clm_node_claim"]}])
    runtime = ToolRuntimeContext(
        execution_id="exe-node",
        conversation_id="conv-node",
        session_id="session-node",
        agent_id="agent-node",
        user_id="user-node",
        model_usage_callback_factory=lambda category: [(category, callback)],
    )

    with tool_runtime_scope(runtime):
        build_agent_node(_Model(), research_final_model=final_model)(_research_final_state())

    assert final_model.calls[0][1] == {"callbacks": [("research_final", callback)]}


def test_research_final_node_falls_back_for_models_without_config():
    class NoConfigSelectionModel:
        def invoke(self, _messages: list[object]) -> object:
            return {"claim_ids": ["clm_node_claim"]}

    result = build_agent_node(
        _Model(), research_final_model=NoConfigSelectionModel()
    )(_research_final_state())

    assert result["messages"][0].content.startswith("## 选题研究摘要")


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
    assert research_result["messages"][0].content.startswith("## 选题研究摘要")
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
