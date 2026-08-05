from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import contentai.services.conversation_service as conversation_service_module
import contentai.services.errors as service_errors
import contentai.services.execution_resume as execution_resume_module
import pytest
from contentai.agent.graph import nodes as graph_nodes
from contentai.agent.runtime.checkpoint import (
    ExecutionScopedCheckpointer,
    RuntimePersistence,
    checkpoint_interrupts,
    checkpoint_messages,
    clear_execution_persistence,
    execution_checkpoint_config,
)
from contentai.agent.runtime.container import RuntimeContainer
from contentai.agent.tools.memory import normalize_remember_input, remember
from contentai.api.chat import (
    _encode_sse_event,
    _http_exception_for_service_error,
    _public_stream_data,
    _to_stream_event_v3,
)
from contentai.core.config import get_settings
from contentai.core.security import AuthContext
from contentai.db.session import get_engine
from contentai.memory.long_term import is_sensitive_memory
from contentai.memory.message_persister import MessagePersister
from contentai.models.base import new_id, utcnow
from contentai.models.chat import (
    AgentExecution,
    AgentInvocation,
    ChatMessage,
    ChatSession,
    ExecutionOutbox,
    ExecutionResumeRequest,
)
from contentai.models.enums import MessageRole, RunStatus
from contentai.models.schemas.chat import AgentMessageRequest, ChatRequest, UserReplyRequest
from contentai.services import tasks as tasks_module
from contentai.services.agent_service import AgentService
from contentai.services.conversation_service import ConversationService
from contentai.services.errors import (
    ChatSessionNotFoundError,
    IdempotencyPayloadMismatchError,
    MessageAlreadyExistsError,
    RunInterruptStaleError,
)
from contentai.services.execution_claim import claim_execution
from contentai.services.execution_lineage import ExecutionLineage
from contentai.services.execution_resume import public_interrupt
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt
from model_config_helpers import DEFAULT_MODEL_CONFIG_ID
from pydantic import ValidationError
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select


def _runtime_container() -> RuntimeContainer:
    class _Model:
        def invoke(self, value: Any) -> Any:
            return value

    class _Gateway:
        def build_agent_model(self, *, tools: list[Any]) -> _Model:
            return _Model()

    return RuntimeContainer(
        settings=get_settings(),
        model_gateway=_Gateway(),
        checkpointer=InMemorySaver(),
    )


def _conversation_service() -> ConversationService:
    return ConversationService(
        AgentService(
            settings=get_settings(),
            runtime=_runtime_container(),
        )
    )


def test_message_contract_rejects_spoofed_agent_and_normalizes_public_payload() -> None:
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(
            {
                "agent_id": "default-agent",
                "message": "hello",
            }
        )

    payload = ChatRequest.model_validate(
        {
            "message": "  hello  ",
            "message_id": "client-message-1",
            "idempotency_key": "request-key",
        }
    )

    assert payload.message == "hello"
    assert payload.message_id == "client-message-1"


@pytest.mark.parametrize(
    "payload",
    [
        {"agent_id": "default-agent", "interrupt_id": "int-1", "decision": "approve"},
        {"interrupt_id": "int-1", "approved": True},
        {"interrupt_id": "int-1", "decision": True},
        {"interrupt_id": "int-1", "decision": "yes"},
        {"interrupt_id": "int-1", "decision": "approve", "message": "please"},
    ],
)
def test_resume_contract_accepts_only_interrupt_id_and_enum_decision(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        UserReplyRequest.model_validate(payload)

    assert UserReplyRequest.model_validate(
        {"interrupt_id": "int-1", "decision": "reject"}
    ).model_dump() == {"interrupt_id": "int-1", "decision": "reject"}


def test_removed_free_text_resume_compatibility_symbols_stay_unreachable() -> None:
    assert not hasattr(execution_resume_module, "stored_resume_value")
    assert not hasattr(service_errors, "ExecutionNotResumableError")
    assert not hasattr(service_errors, "ExecutionResumeValueRequiredError")


def test_runtime_config_uses_execution_scoped_checkpoint_namespace() -> None:
    runtime = _runtime_container().create_runtime(
        model_config_id=DEFAULT_MODEL_CONFIG_ID,
        tool_permissions=(),
        user_id="user-1",
        agent_id="agent-1",
        session_id="thread-1",
        conversation_id="session-1",
        execution_id="execution-1",
    )

    configurable = runtime.config["configurable"]
    assert configurable["thread_id"] == "thread-1"
    assert configurable["checkpoint_ns"] == ""
    assert configurable["execution_id"] == "execution-1"


def test_execution_scoped_checkpointer_fails_closed_and_isolates_sync_and_async_graphs() -> None:
    thread_id = "thread-task2-memory-isolation"
    config_a = execution_checkpoint_config(thread_id=thread_id, execution_id="execution-a")
    config_b = execution_checkpoint_config(thread_id=thread_id, execution_id="execution-b")
    delegate = InMemorySaver()
    checkpointer = ExecutionScopedCheckpointer(delegate)

    def reply_node(state: MessagesState) -> dict[str, Any]:
        content = str(state["messages"][-1].content)
        return {"messages": [AIMessage(content=f"reply:{content}")]}

    builder = StateGraph(MessagesState)
    builder.add_node("reply", reply_node)
    builder.add_edge(START, "reply")
    builder.add_edge("reply", END)
    graph = builder.compile(checkpointer=checkpointer)

    with pytest.raises(ValueError, match="execution_id"):
        checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})
    with pytest.raises(ValueError, match="execution_id"):
        checkpointer.get_tuple(
            {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        )
    with pytest.raises(ValueError, match="execution_id"):
        checkpointer.get_tuple(
            {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": "execution-a",
                }
            }
        )
    with pytest.raises(ValueError, match="execution_id"):
        list(checkpointer.list(None))
    with pytest.raises(ValueError, match="execution_id"):
        list(
            graph.stream(
                {"messages": [("user", "legacy-empty-namespace")]},
                config={"configurable": {"thread_id": thread_id}},
            )
        )

    list(graph.stream({"messages": [("user", "sync-a")]}, config=config_a))
    state_a = graph.get_state(config_a)
    history_a = list(graph.get_state_history(config_a))
    assert state_a.values["messages"][-1].content == "reply:sync-a"
    assert history_a
    assert all(
        snapshot.config["configurable"]["execution_id"] == "execution-a"
        for snapshot in history_a
    )
    assert [message.content for message in checkpoint_messages(
        checkpointer, thread_id=thread_id, execution_id="execution-a"
    )][-1] == "reply:sync-a"
    assert checkpoint_messages(
        checkpointer, thread_id=thread_id, execution_id="execution-b"
    ) == []

    async def exercise_async_paths() -> None:
        async for _ in graph.astream(
            {"messages": [("user", "async-b")]},
            config=config_b,
        ):
            pass
        tuple_a = await checkpointer.aget_tuple(config_a)
        tuple_b = await checkpointer.aget_tuple(config_b)
        async_state_b = await graph.aget_state(config_b)
        async_history_b = [snapshot async for snapshot in graph.aget_state_history(config_b)]
        assert tuple_a is not None
        assert tuple_b is not None
        assert tuple_a.config["configurable"]["execution_id"] == "execution-a"
        assert tuple_b.config["configurable"]["execution_id"] == "execution-b"
        assert tuple_a.config["configurable"]["checkpoint_ns"] == ""
        assert tuple_b.config["configurable"]["checkpoint_ns"] == ""
        assert async_state_b.values["messages"][-1].content == "reply:async-b"
        assert async_history_b
        assert all(
            snapshot.config["configurable"]["execution_id"] == "execution-b"
            for snapshot in async_history_b
        )
        assert [message.content for message in checkpoint_messages(
            checkpointer, thread_id=thread_id, execution_id="execution-a"
        )][-1] == "reply:sync-a"
        assert [message.content for message in checkpoint_messages(
            checkpointer, thread_id=thread_id, execution_id="execution-b"
        )][-1] == "reply:async-b"
        with pytest.raises(ValueError, match="execution_id"):
            await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
        with pytest.raises(ValueError, match="execution_id"):
            await checkpointer.aget_tuple(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": "execution-a",
                    }
                }
            )
        with pytest.raises(ValueError, match="execution_id"):
            async for _ in checkpointer.alist(None):
                pass
        with pytest.raises(ValueError, match="execution-scoped"):
            await checkpointer.adelete_thread(thread_id)

    import asyncio

    asyncio.run(exercise_async_paths())
    assert set(delegate.storage[thread_id]) == {"execution-a", "execution-b"}
    with pytest.raises(ValueError, match="execution-scoped"):
        checkpointer.delete_thread(thread_id)
    assert checkpointer.get_tuple(config_a) is not None
    assert checkpointer.get_tuple(config_b) is not None
    checkpointer.delete_namespace(thread_id, "execution-a")
    assert checkpointer.get_tuple(config_a) is None
    assert checkpointer.get_tuple(config_b) is not None
    checkpointer.delete_namespace(thread_id, "execution-b")
    assert checkpointer.get_tuple(config_b) is None


def test_human_node_consumes_only_structured_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        graph_nodes,
        "interrupt",
        lambda _value: {"decision": "reject"},
    )
    node = graph_nodes.build_human_node()

    result = node({"messages": []})

    assert result["human_approved"] is False
    assert result["messages"][-1].content == "已取消保存"


def test_public_interrupt_event_recursively_exposes_only_allowlisted_fields() -> None:
    projected = _public_stream_data(
        {
            "name": "run_interrupt",
            "interrupt": {
                "interrupt_id": "int-public",
                "actions": [
                    {
                        "tool_name": "remember",
                        "purpose": "保存一条长期记忆",
                        "memory": {"type": "preference", "content": "简洁回复"},
                        "tool_call_id": "call-secret-1",
                        "args": {"api_key": "secret-1"},
                    },
                    {
                        "tool_name": "search",
                        "purpose": "运行工具 search",
                        "memory": None,
                        "tool_call_id": "call-secret-2",
                        "runtime": {"permissions": ["*"]},
                    },
                ],
                "execution_id": "exe-secret",
                "task_id": "task-secret",
            },
        },
        channel="interrupts",
    )

    assert projected == {
        "name": "run_interrupt",
        "interrupt": {
            "interrupt_id": "int-public",
            "actions": [
                {
                    "tool_name": "remember",
                    "purpose": "保存一条长期记忆",
                    "memory": {"type": "preference", "content": "简洁回复"},
                },
                {
                    "tool_name": "search",
                    "purpose": "运行工具 search",
                },
            ],
        },
    }


@pytest.mark.parametrize(
    "channel",
    ["lifecycle", "values", "messages", "tools", "interrupts"],
)
@pytest.mark.parametrize("variant", ["raw", "legacy", "malformed"])
def test_interrupt_stream_sanitization_is_channel_independent_and_total(
    channel: str,
    variant: str,
) -> None:
    raw = {
        "interrupts": [
            {
                "id": "int-cross-channel",
                "value": {
                    "tool_calls": [
                        {
                            "name": "remember",
                            "id": "call-secret",
                            "args": {
                                "content": "public preference",
                                "kind": "preference",
                                "api_key": "secret-argument",
                            },
                        }
                    ],
                    "runtime": {"access_token": "secret-runtime"},
                },
            }
        ]
    }
    interrupt_value: Any
    if variant == "raw":
        interrupt_value = raw
    elif variant == "legacy":
        interrupt_value = {
            "interrupt_id": "int-legacy",
            "tool_name": "remember",
            "purpose": "legacy",
            "args": {"api_key": "secret-legacy"},
        }
    else:
        interrupt_value = {
            "interrupt_id": "int-malformed",
            "actions": [{"tool_name": "x" * 256, "purpose": "unsafe"}],
            "runtime": {"private_key": "secret-malformed"},
        }

    projected = _public_stream_data(
        {
            "name": "run_interrupt",
            "interrupt": interrupt_value,
            "tool_call_id": "call-top-secret",
            "content": '{"args":{"api_key":"secret-sibling"}}',
        },
        channel=channel,
    )
    encoded = json.dumps(projected, ensure_ascii=False)

    if variant == "raw":
        assert projected["interrupt"] == {
            "interrupt_id": "int-cross-channel",
            "actions": [
                {
                    "tool_name": "remember",
                    "purpose": "保存一条长期记忆",
                    "memory": {"type": "preference", "content": "public preference"},
                }
            ],
        }
    else:
        assert projected["interrupt"] is None
    assert "tool_call_id" not in projected
    for forbidden in (
        "call-secret",
        "api_key",
        "runtime",
        "access_token",
        "private_key",
        "secret-sibling",
    ):
        assert forbidden not in encoded


def test_interrupt_sse_envelope_drops_call_ids_and_raw_sibling_fields() -> None:
    stream_event = _to_stream_event_v3(
        "tool_progress",
        {
            "execution_id": "execution-public",
            "sequence": 1,
            "name": "run_interrupt",
            "namespace": ["langgraph-task-secret"],
            "tool_call_id": "call-envelope-secret",
            "content": '{"args":{"api_key":"secret-sibling"}}',
            "interrupt": {
                "interrupts": [
                    {
                        "id": "int-public",
                        "value": {
                            "tool_calls": [
                                {
                                    "id": "call-inner-secret",
                                    "name": "remember",
                                    "args": {
                                        "content": "public preference",
                                        "kind": "preference",
                                        "api_key": "secret-argument",
                                    },
                                }
                            ],
                            "runtime": {"access_token": "secret-runtime"},
                        },
                    }
                ]
            },
        },
    )
    encoded = _encode_sse_event(
        stream_event.channel,
        stream_event.model_dump(mode="json"),
        event_id=stream_event.event_id,
    )

    assert stream_event.namespace == ()
    assert stream_event.tool_call_id is None
    assert stream_event.data == {
        "name": "run_interrupt",
        "interrupt": {
            "interrupt_id": "int-public",
            "actions": [
                {
                    "tool_name": "remember",
                    "purpose": "保存一条长期记忆",
                    "memory": {"type": "preference", "content": "public preference"},
                }
            ],
        },
    }
    for forbidden in (
        "call-envelope-secret",
        "call-inner-secret",
        "langgraph-task-secret",
        "secret-sibling",
        "secret-argument",
        "secret-runtime",
        "api_key",
        "access_token",
    ):
        assert forbidden not in encoded


def test_remember_kind_null_matches_structured_tool_rejection() -> None:
    validated = remember.args_schema.model_validate({"content": "safe memory"})
    assert validated.kind == "semantic"
    with pytest.raises(ValidationError):
        remember.args_schema.model_validate({"content": "safe memory", "kind": None})
    with pytest.raises(ValidationError):
        remember.args_schema.model_validate(
            {"content": "safe memory", "kind": b"preference"}
        )
    assert normalize_remember_input("safe memory", None) is None
    assert normalize_remember_input("safe memory", b"preference") is None


@pytest.mark.parametrize(
    "content",
    [
        "my API key is abc123",
        "my API     key is abc123",
        "client secret abc123",
        "access token abc123",
        "private key abc123",
        "OPENAI_API_KEY=opaque-api-value",
        "google_client_secret = opaque-client-value",
        "GitHub-Access-Token: opaque-access-value",
        "RSA PRIVATE KEY = opaque-private-value",
        "googleClientSecret=opaque-camel-value",
    ],
)
def test_sensitive_memory_patterns_cover_spaced_credential_labels(content: str) -> None:
    assert is_sensitive_memory(content)
    payload = {
        "interrupts": [
            {
                "id": "int-sensitive",
                "value": {
                    "tool_calls": [
                        {
                            "name": "remember",
                            "args": {"content": content, "kind": "preference"},
                        }
                    ]
                },
            }
        ]
    }
    assert public_interrupt(payload) is None
    stream_event = _to_stream_event_v3(
        "state",
        {
            "execution_id": "execution-sensitive",
            "sequence": 1,
            "name": "run_interrupt",
            "interrupt": payload,
        },
    )
    encoded = _encode_sse_event(
        stream_event.channel,
        stream_event.model_dump(mode="json"),
        event_id=stream_event.event_id,
    )
    assert stream_event.data == {"name": "run_interrupt", "interrupt": None}
    assert content not in encoded


@pytest.mark.parametrize(
    "content",
    [
        "please tokenize this paragraph",
        "the tokenizer is deterministic",
        "contact the secretary",
    ],
)
def test_sensitive_memory_patterns_avoid_obvious_word_substring_false_positives(
    content: str,
) -> None:
    assert not is_sensitive_memory(content)


def test_multiple_top_level_interrupts_fail_closed_as_one_approval_scope() -> None:
    payload = {
        "interrupts": [
            {
                "id": "int-first",
                "value": {
                    "tool_calls": [
                        {"name": "remember", "args": {"content": "visible first"}}
                    ]
                },
            },
            {
                "id": "int-hidden",
                "value": {
                    "tool_calls": [
                        {
                            "name": "remember",
                            "args": {"content": "hidden second", "api_key": "secret"},
                        }
                    ]
                },
            },
        ]
    }

    assert public_interrupt(payload) is None
    assert execution_resume_module.interrupt_identity(payload)[0] == ""
    assert _public_stream_data(
        {"name": "run_interrupt", "interrupt": payload},
        channel="values",
    )["interrupt"] is None


def test_public_interrupt_normalizes_remember_exactly_like_tool_execution() -> None:
    content = "  " + ("记 忆 " * 400) + "  "
    payload = {
        "interrupts": [
            {
                "id": "int-normalized-remember",
                "value": {
                    "tool_calls": [
                        {
                            "name": "remember",
                            "args": {
                                "kind": "NOT-A-REAL-KIND" * 20,
                                "content": json.dumps(
                                    {"content": content, "api_key": "nested-secret"},
                                    ensure_ascii=False,
                                ),
                                "runtime": {"token": "secret"},
                            },
                            "id": "call-secret",
                        }
                    ]
                },
            }
        ]
    }

    projected = public_interrupt(payload)

    assert projected is not None
    assert projected.interrupt_id == "int-normalized-remember"
    assert len(projected.actions) == 1
    assert projected.actions[0].tool_name == "remember"
    assert projected.actions[0].memory is not None
    assert projected.actions[0].memory.type == "semantic"
    assert len(projected.actions[0].memory.content) <= 1000
    assert projected.actions[0].memory.content == projected.actions[0].memory.content.strip()
    assert "secret" not in projected.model_dump_json()


@pytest.mark.parametrize(
    "tool_calls",
    [
        [{"name": "x" * 256, "args": {}}],
        [{"name": "remember", "args": {"kind": "preference"}}],
        [{"name": {"malformed": True}, "args": {}}],
        [{"name": "remember", "args": "not-an-object"}],
        ["not-a-tool-call"],
        [
            {
                "name": "remember",
                "args": {
                    "content": '{"provider":"internal","runtime":"opaque"}',
                    "kind": "semantic",
                },
            }
        ],
        [
            {
                "name": "remember",
                "args": {
                    "content": '{"content":{"nested":"no"},"api_key":"leak-me"}',
                    "kind": "semantic",
                },
            }
        ],
        [
            {
                "name": "remember",
                "args": {"content": '"json scalar"', "kind": "semantic"},
            }
        ],
        [
            {
                "name": "remember",
                "args": {"content": '["provider-secret"]', "kind": "semantic"},
            }
        ],
        [
            {
                "name": "remember",
                "args": {"content": '[42,"scalar-secret"]', "kind": "semantic"},
            }
        ],
        [
            {
                "name": "remember",
                "args": {"content": "null", "kind": "semantic"},
            }
        ],
        [
            {
                "name": "remember",
                "args": {"content": "safe memory", "kind": None},
            }
        ],
    ],
)
def test_public_interrupt_is_total_and_fails_closed_for_unsafe_actions(
    tool_calls: list[Any],
) -> None:
    payload = {
        "interrupts": [
            {
                "id": "int-unsafe",
                "value": {
                    "tool_calls": tool_calls,
                    "runtime": {"api_key": "top-secret"},
                },
            }
        ]
    }

    assert public_interrupt(payload) is None
    projected = _public_stream_data(
        {"name": "run_interrupt", "interrupt": payload},
        channel="interrupts",
    )
    assert projected == {"name": "run_interrupt", "interrupt": None}
    assert "secret" not in json.dumps(projected, ensure_ascii=False)


def test_invocation_request_digest_column_is_present_in_real_postgres() -> None:
    columns = {column["name"] for column in inspect(get_engine()).get_columns("agentinvocation")}

    assert "request_sha256" in columns
    assert AgentInvocation.__table__.c.request_sha256.type.length == 64


def test_unclaimed_watchdog_uses_publish_time_not_execution_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings().model_copy(deep=True)
    settings.agent.worker_claim_timeout_seconds = 30
    now = utcnow()
    with Session(get_engine(settings)) as session:
        chat = ChatSession(
            id="session-watchdog-publish-time",
            agent_id="default-agent",
            agent_version_id="default-agent-v1",
            user_id="local-user",
        )
        session.add(chat)
        session.flush()
        invocation = AgentInvocation(
            id="invocation-watchdog-publish-time",
            session_id=chat.id,
            agent_id=chat.agent_id,
            user_id=chat.user_id,
        )
        session.add(invocation)
        session.flush()
        execution = AgentExecution(
            id="execution-watchdog-publish-time",
            invocation_id=invocation.id,
            session_id=chat.id,
            agent_version_id=chat.agent_version_id,
            model_config_id=DEFAULT_MODEL_CONFIG_ID,
            created_at=now - timedelta(hours=1),
        )
        session.add(execution)
        session.flush()
        session.add(
            ExecutionOutbox(
                execution_id=execution.id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
                kind="execute",
                status="published",
                published_at=now - timedelta(seconds=5),
            )
        )
        session.commit()

    monkeypatch.setattr(tasks_module, "get_settings", lambda: settings)

    assert tasks_module.recover_expired_executions.run() == 0
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, "execution-watchdog-publish-time")
        assert execution is not None
        assert execution.status == RunStatus.pending
        assert execution.finished_at is None


def _seed_chat(session: Session, *, suffix: str) -> ChatSession:
    chat = ChatSession(
        id=f"session-{suffix}",
        agent_id="default-agent",
        agent_version_id="default-agent-v1",
        user_id="local-user",
    )
    session.add(chat)
    session.commit()
    session.refresh(chat)
    return chat


def test_create_turn_does_not_translate_non_integrity_commit_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _conversation_service()
    auth = AuthContext(user_id="local-user", allowed_agent_ids=("default-agent",))
    with Session(get_engine()) as session:
        chat = _seed_chat(session, suffix="commit-runtime-error")

        original_rollback = session.rollback
        rollback_calls = 0

        def fail_commit() -> None:
            raise RuntimeError("database unavailable")

        def track_rollback() -> None:
            nonlocal rollback_calls
            rollback_calls += 1
            original_rollback()

        monkeypatch.setattr(session, "commit", fail_commit)
        monkeypatch.setattr(session, "rollback", track_rollback)
        with pytest.raises(RuntimeError, match="database unavailable"):
            service.create_turn(
                session,
                AgentMessageRequest(
                    session_id=chat.id,
                    message="commit failure should remain visible",
                ),
                auth,
            )
        assert rollback_calls == 1


def test_create_turn_maps_reused_client_message_id_to_stable_conflict() -> None:
    service = _conversation_service()
    auth = AuthContext(user_id="local-user", allowed_agent_ids=("default-agent",))
    with Session(get_engine()) as session:
        chat = _seed_chat(session, suffix="duplicate-message-id")
        first, replayed = service.create_turn(
            session,
            AgentMessageRequest(
                session_id=chat.id,
                message="first message",
                message_id="client-duplicate-message-id",
            ),
            auth,
        )
        assert replayed is False
        execution = session.get(AgentExecution, first.execution_id)
        assert execution is not None
        execution.status = RunStatus.completed
        session.add(execution)
        session.commit()

        with pytest.raises(MessageAlreadyExistsError, match="already in use"):
            service.create_turn(
                session,
                AgentMessageRequest(
                    session_id=chat.id,
                    message="different message",
                    message_id="client-duplicate-message-id",
                ),
                auth,
            )

        # The service rolled back the failed flush/commit, so callers can
        # immediately reuse the same Session for another query.
        assert session.get(ChatSession, chat.id) is not None


def test_create_turn_rolls_back_non_message_integrity_failure_without_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _conversation_service()
    auth = AuthContext(user_id="local-user", allowed_agent_ids=("default-agent",))
    duplicate_invocation_id = "duplicate-invocation-id"
    message_id = "message-for-invocation-conflict"

    with Session(get_engine()) as seed_session:
        chat = _seed_chat(seed_session, suffix="duplicate-invocation-id")
        chat_id = chat.id
        seed_session.add(
            AgentInvocation(
                id=duplicate_invocation_id,
                session_id=chat_id,
                agent_id=chat.agent_id,
                user_id=auth.user_id,
            )
        )
        seed_session.commit()

    def controlled_new_id(prefix: str) -> str:
        if prefix == "inv":
            return duplicate_invocation_id
        return new_id(prefix)

    monkeypatch.setattr(conversation_service_module, "new_id", controlled_new_id)

    with Session(get_engine()) as session:
        with pytest.raises(IntegrityError) as caught:
            service.create_turn(
                session,
                AgentMessageRequest(
                    session_id=chat_id,
                    message="preserve the original database error",
                    message_id=message_id,
                ),
                auth,
            )

        assert service._integrity_constraint_name(caught.value) == "agentinvocation_pkey"
        # The aggregate flush failed before the message insert. Rollback still
        # restores the caller-owned Session and preserves the original row.
        assert session.get(AgentInvocation, duplicate_invocation_id) is not None
        assert session.get(ChatMessage, message_id) is None


def test_message_already_exists_maps_to_stable_http_conflict() -> None:
    response = _http_exception_for_service_error(
        MessageAlreadyExistsError("The message_id is already in use."),
        request_id="request-message-conflict",
        session_id="session-message-conflict",
    )

    assert response.status_code == 409
    assert response.detail == {
        "code": "MESSAGE_ALREADY_EXISTS",
        "message": "The message_id is already in use.",
        "request_id": "request-message-conflict",
        "session_id": "session-message-conflict",
    }


def test_execution_lineage_locks_owned_session_and_derives_all_identity() -> None:
    with Session(get_engine()) as session:
        chat = _seed_chat(session, suffix="lineage")

        lineage = ExecutionLineage.resolve_for_update(
            session,
            chat.id,
            AuthContext(user_id="local-user", allowed_agent_ids=("default-agent",)),
        )

        assert lineage.chat is chat
        assert lineage.session_id == chat.id
        assert lineage.user_id == "local-user"
        assert lineage.agent_id == "default-agent"
        assert lineage.agent_version_id == "default-agent-v1"


def test_execution_lineage_does_not_lock_another_users_session() -> None:
    with Session(get_engine()) as owner_session:
        chat = _seed_chat(owner_session, suffix="lineage-other-user")

    with Session(get_engine()) as unauthorized_session:
        with pytest.raises(ChatSessionNotFoundError):
            ExecutionLineage.resolve_for_update(
                unauthorized_session,
                chat.id,
                AuthContext(user_id="other-user", allowed_agent_ids=("default-agent",)),
            )

        with Session(get_engine()) as concurrent_session:
            locked = concurrent_session.exec(
                select(ChatSession)
                .where(ChatSession.id == chat.id)
                .with_for_update(nowait=True)
            ).one()
            assert locked.id == chat.id


def test_idempotency_key_is_bound_to_normalized_request_payload() -> None:
    service = _conversation_service()
    auth = AuthContext(user_id="local-user", allowed_agent_ids=("default-agent",))
    with Session(get_engine()) as session:
        chat = _seed_chat(session, suffix="digest")
        first, replayed = service.create_turn(
            session,
            AgentMessageRequest(
                session_id=chat.id,
                message="  canonical message  ",
                message_id="client-message-digest",
            ),
            auth,
            idempotency_key="digest-key",
        )
        replay, was_replayed = service.create_turn(
            session,
            AgentMessageRequest(
                session_id=chat.id,
                message="canonical message",
                message_id="client-message-digest",
            ),
            auth,
            idempotency_key="digest-key",
        )

        assert replayed is False
        assert was_replayed is True
        assert replay.message_id == first.message_id
        assert replay.execution_id == first.execution_id

        with pytest.raises(IdempotencyPayloadMismatchError):
            service.create_turn(
                session,
                AgentMessageRequest(
                    session_id=chat.id,
                    message="different message",
                    message_id="client-message-digest",
                ),
                auth,
                idempotency_key="digest-key",
            )


def test_idempotency_replay_keeps_original_message_after_approval() -> None:
    service = _conversation_service()
    auth = AuthContext(user_id="local-user", allowed_agent_ids=("default-agent",))
    with Session(get_engine()) as session:
        chat = _seed_chat(session, suffix="approval-idempotency-replay")
        payload = AgentMessageRequest(
            session_id=chat.id,
            message="remember this after approval",
            message_id="client-message-approval-replay",
        )
        first, replayed = service.create_turn(
            session,
            payload,
            auth,
            idempotency_key="approval-replay-key",
        )
        assert replayed is False

        execution = session.get(AgentExecution, first.execution_id)
        assert execution is not None
        execution.status = RunStatus.waiting_input
        execution.interrupt_payload = {
            "interrupts": [
                {
                    "id": "interrupt-approval-replay",
                    "value": {
                        "tool_calls": [
                            {
                                "name": "remember",
                                "id": "call-approval-replay",
                                "args": {"content": "remember this after approval"},
                            }
                        ]
                    },
                }
            ]
        }
        session.add(execution)
        session.commit()

        service.resume_execution(
            session,
            execution.id,
            "interrupt-approval-replay",
            "approve",
            auth,
        )
        decision_message = session.exec(
            select(ChatMessage).where(ChatMessage.execution_id == execution.id)
        ).one()

        replay, was_replayed = service.create_turn(
            session,
            payload,
            auth,
            idempotency_key="approval-replay-key",
        )

        assert was_replayed is True
        assert replay.message_id == first.message_id
        assert replay.message_id != decision_message.id
        assert replay.execution_id == first.execution_id


def test_historical_key_without_digest_is_not_replayable() -> None:
    service = _conversation_service()
    auth = AuthContext(user_id="local-user", allowed_agent_ids=("default-agent",))
    with Session(get_engine()) as session:
        chat = _seed_chat(session, suffix="historical-digest")
        invocation = AgentInvocation(
            session_id=chat.id,
            agent_id=chat.agent_id,
            user_id=chat.user_id,
            idempotency_key="historical-key",
            request_sha256=None,
        )
        session.add(invocation)
        session.flush()
        session.add(
            ChatMessage(
                session_id=chat.id,
                invocation_id=invocation.id,
                role=MessageRole.user,
                content="old payload",
            )
        )
        session.flush()
        session.add(
            AgentExecution(
                invocation_id=invocation.id,
                session_id=chat.id,
                agent_version_id=chat.agent_version_id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
            )
        )
        session.commit()

        with pytest.raises(IdempotencyPayloadMismatchError):
            service.create_turn(
                session,
                AgentMessageRequest(session_id=chat.id, message="old payload"),
                auth,
                idempotency_key="historical-key",
            )


@pytest.mark.parametrize(
    ("decision", "expected_content"),
    [
        ("approve", "已批准工具执行。"),
        ("reject", "已拒绝工具执行。"),
    ],
)
def test_resume_acceptance_is_one_transaction_with_one_decision_message(
    decision: str,
    expected_content: str,
) -> None:
    service = _conversation_service()
    auth = AuthContext(user_id="local-user", allowed_agent_ids=("default-agent",))
    with Session(get_engine()) as session:
        chat = _seed_chat(session, suffix=f"resume-{decision}")
        invocation = AgentInvocation(
            session_id=chat.id,
            agent_id=chat.agent_id,
            user_id=chat.user_id,
        )
        session.add(invocation)
        session.flush()
        session.add(
            ChatMessage(
                session_id=chat.id,
                invocation_id=invocation.id,
                role=MessageRole.user,
                content="remember this",
            )
        )
        session.flush()
        execution = AgentExecution(
            invocation_id=invocation.id,
            session_id=chat.id,
            agent_version_id=chat.agent_version_id,
            model_config_id=DEFAULT_MODEL_CONFIG_ID,
            status=RunStatus.waiting_input,
            interrupt_payload={
                "interrupts": [
                    {
                        "id": f"interrupt-{decision}",
                        "value": {
                            "tool_calls": [
                                {
                                    "name": "remember",
                                    "id": "call-private",
                                    "args": {"content": "remember this"},
                                }
                            ]
                        },
                    }
                ]
            },
        )
        session.add(execution)
        session.flush()
        session.add(
            ExecutionOutbox(
                execution_id=execution.id,
                model_config_id=execution.model_config_id,
                kind="execute",
            )
        )
        session.commit()

        response = service.resume_execution(
            session,
            execution.id,
            f"interrupt-{decision}",
            decision,
            auth,
        )

        assert response.status == "pending"
        requests = list(
            session.exec(
                select(ExecutionResumeRequest).where(
                    ExecutionResumeRequest.execution_id == execution.id
                )
            ).all()
        )
        assert len(requests) == 1
        assert requests[0].decision == decision
        assert requests[0].value == {"decision": decision}
        decision_messages = list(
            session.exec(
                select(ChatMessage).where(ChatMessage.execution_id == execution.id)
            ).all()
        )
        assert [message.content for message in decision_messages] == [expected_content]
        assert requests[0].message_id == decision_messages[0].id

        with pytest.raises(RunInterruptStaleError):
            service.resume_execution(
                session,
                execution.id,
                f"interrupt-{decision}",
                decision,
                auth,
            )

        cancelled = service.cancel_execution(session, execution.id, auth)
        session.refresh(requests[0])
        assert cancelled.status == "cancelled"
        assert requests[0].status == "stale"


def test_real_postgres_checkpoint_namespace_hides_old_interrupt_from_new_execution() -> None:
    thread_id = "thread-task2-real-checkpoint"
    old_execution_id = "execution-task2-old-interrupt"
    new_execution_id = "execution-task2-new-run"
    persistence = RuntimePersistence(get_settings())
    checkpointer = persistence.get_checkpointer()

    class _State(MessagesState):
        approved: bool

    def approval_node(_state: _State) -> dict[str, Any]:
        reply = interrupt({"tool_calls": [{"name": "remember", "args": {"content": "x"}}]})
        return {"approved": reply == {"decision": "approve"}}

    graph_builder = StateGraph(_State)
    graph_builder.add_node("approval", approval_node)
    graph_builder.add_edge(START, "approval")
    graph_builder.add_edge("approval", END)
    graph = graph_builder.compile(checkpointer=checkpointer)
    old_config = execution_checkpoint_config(
        thread_id=thread_id,
        execution_id=old_execution_id,
    )

    try:
        list(graph.stream({"messages": []}, config=old_config))

        assert checkpoint_interrupts(
            checkpointer,
            thread_id=thread_id,
            execution_id=old_execution_id,
        )
        assert checkpoint_interrupts(
            checkpointer,
            thread_id=thread_id,
            execution_id=new_execution_id,
        ) == []
    finally:
        clear_execution_persistence(
            thread_id=thread_id,
            execution_id=old_execution_id,
            checkpointer=checkpointer,
        )
        clear_execution_persistence(
            thread_id=thread_id,
            execution_id=new_execution_id,
            checkpointer=checkpointer,
        )
        persistence.close()


def test_final_assistant_materialization_insert_or_reads_one_execution_message() -> None:
    with Session(get_engine()) as session:
        chat = _seed_chat(session, suffix="assistant-once")
        invocation = AgentInvocation(
            session_id=chat.id,
            agent_id=chat.agent_id,
            user_id=chat.user_id,
        )
        session.add(invocation)
        session.flush()
        execution = AgentExecution(
            invocation_id=invocation.id,
            session_id=chat.id,
            agent_version_id=chat.agent_version_id,
            model_config_id=DEFAULT_MODEL_CONFIG_ID,
        )
        session.add(execution)
        session.commit()

        persister = MessagePersister()
        first = persister.persist_assistant_text(
            session,
            session_id=chat.id,
            invocation_id=invocation.id,
            execution_id=execution.id,
            content="checkpoint final",
        )
        second = persister.persist_assistant_text(
            session,
            session_id=chat.id,
            invocation_id=invocation.id,
            execution_id=execution.id,
            content="must not replace checkpoint final",
        )
        session.commit()

        rows = list(
            session.exec(
                select(ChatMessage).where(
                    ChatMessage.execution_id == execution.id,
                    ChatMessage.role == MessageRole.assistant,
                )
            ).all()
        )
        assert first.id == second.id
        assert len(rows) == 1
        assert rows[0].content == "checkpoint final"


def test_end_checkpoint_is_recoverable_without_replaying_graph_side_effect() -> None:
    thread_id = "thread-task2-end-recovery"
    execution_id = "execution-task2-end-recovery"
    side_effects: list[str] = []
    persistence = RuntimePersistence(get_settings())
    checkpointer = persistence.get_checkpointer()

    def final_node(_state: MessagesState) -> dict[str, Any]:
        side_effects.append("called")
        return {"messages": [AIMessage(content="durable final answer")]}

    graph_builder = StateGraph(MessagesState)
    graph_builder.add_node("final", final_node)
    graph_builder.add_edge(START, "final")
    graph_builder.add_edge("final", END)
    graph = graph_builder.compile(checkpointer=checkpointer)
    config = execution_checkpoint_config(thread_id=thread_id, execution_id=execution_id)

    try:
        list(graph.stream({"messages": []}, config=config))
        for _ in range(3):
            assert list(graph.stream(None, config=config)) == []
            recovered = checkpoint_messages(
                checkpointer,
                thread_id=thread_id,
                execution_id=execution_id,
            )
            assert recovered[-1].content == "durable final answer"
        assert side_effects == ["called"]
    finally:
        clear_execution_persistence(
            thread_id=thread_id,
            execution_id=execution_id,
            checkpointer=checkpointer,
        )
        persistence.close()


def test_concurrent_idempotent_submissions_create_one_complete_turn_aggregate() -> None:
    auth = AuthContext(user_id="local-user", allowed_agent_ids=("default-agent",))
    with Session(get_engine()) as session:
        chat = _seed_chat(session, suffix="concurrent-idempotency")
        session_id = chat.id

    def submit() -> tuple[str, str, bool]:
        with Session(get_engine()) as session:
            response, replayed = _conversation_service().create_turn(
                session,
                AgentMessageRequest(
                    session_id=session_id,
                    message="one concurrent request",
                    message_id="client-concurrent-message",
                ),
                auth,
                idempotency_key="concurrent-key",
            )
            return response.message_id, response.execution_id, replayed

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: submit(), range(2)))

    assert {result[0] for result in results} == {"client-concurrent-message"}
    assert len({result[1] for result in results}) == 1
    assert sorted(result[2] for result in results) == [False, True]
    with Session(get_engine()) as session:
        assert len(
            session.exec(
                select(AgentInvocation).where(AgentInvocation.session_id == session_id)
            ).all()
        ) == 1
        assert len(
            session.exec(
                select(AgentExecution).where(AgentExecution.session_id == session_id)
            ).all()
        ) == 1
        assert len(
            session.exec(select(ChatMessage).where(ChatMessage.session_id == session_id)).all()
        ) == 1
        assert len(
            session.exec(
                select(ExecutionOutbox).join(
                    AgentExecution,
                    ExecutionOutbox.execution_id == AgentExecution.id,
                ).where(AgentExecution.session_id == session_id)
            ).all()
        ) == 1


def test_claim_fence_rejects_mismatched_aggregate_before_runtime_access() -> None:
    settings = get_settings().model_copy(deep=True)

    class _ForbiddenRuntime:
        def get_checkpointer(self) -> Any:
            raise AssertionError("runtime must not be touched for a mismatched aggregate")

    with Session(get_engine(settings)) as session:
        first = _seed_chat(session, suffix="claim-fence-first")
        second = _seed_chat(session, suffix="claim-fence-second")
        invocation = AgentInvocation(
            session_id=first.id,
            agent_id=first.agent_id,
            user_id=first.user_id,
        )
        session.add(invocation)
        session.flush()
        execution = AgentExecution(
            id="execution-claim-scope-mismatch",
            invocation_id=invocation.id,
            session_id=first.id,
            agent_version_id=first.agent_version_id,
            model_config_id=DEFAULT_MODEL_CONFIG_ID,
        )
        session.add(execution)
        session.commit()
        session.exec(text("SET session_replication_role = replica"))
        session.exec(
            text("UPDATE agentexecution SET session_id = :session_id WHERE id = :id"),
            params={"session_id": second.id, "id": execution.id},
        )
        session.exec(text("SET session_replication_role = origin"))
        session.commit()

    claimed = claim_execution(
        SimpleNamespace(settings=settings, runtime=_ForbiddenRuntime()),
        "execution-claim-scope-mismatch",
        "worker-mismatch",
    )

    assert claimed is None
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, "execution-claim-scope-mismatch")
        assert execution is not None
        assert execution.status == RunStatus.failed
        assert execution.error == "EXECUTION_SCOPE_MISMATCH"


def test_claim_and_watchdog_row_fence_have_exactly_one_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings().model_copy(deep=True)
    settings.agent.worker_claim_timeout_seconds = 1
    with Session(get_engine(settings)) as session:
        chat = _seed_chat(session, suffix="claim-watchdog-race")
        invocation = AgentInvocation(
            session_id=chat.id,
            agent_id=chat.agent_id,
            user_id=chat.user_id,
        )
        session.add(invocation)
        session.flush()
        execution = AgentExecution(
            id="execution-claim-watchdog-race",
            invocation_id=invocation.id,
            session_id=chat.id,
            agent_version_id=chat.agent_version_id,
            model_config_id=DEFAULT_MODEL_CONFIG_ID,
        )
        session.add(execution)
        session.flush()
        session.add(
            ExecutionOutbox(
                execution_id=execution.id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
                status="published",
                published_at=utcnow() - timedelta(seconds=5),
            )
        )
        session.commit()

    monkeypatch.setattr(tasks_module, "get_settings", lambda: settings)
    service = SimpleNamespace(settings=settings)
    with ThreadPoolExecutor(max_workers=2) as executor:
        claim_future = executor.submit(
            claim_execution,
            service,
            "execution-claim-watchdog-race",
            "worker-race",
        )
        watchdog_future = executor.submit(tasks_module.recover_expired_executions.run)
        claimed = claim_future.result()
        watchdog_future.result()

    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, "execution-claim-watchdog-race")
        assert execution is not None
        claim_won = claimed is not None and execution.worker_id == "worker-race"
        watchdog_won = execution.status == RunStatus.failed
        assert claim_won is not watchdog_won
