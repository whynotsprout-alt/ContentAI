from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from agent.graph import nodes as graph_nodes
from agent.runtime.checkpoint import (
    RuntimePersistence,
    checkpoint_interrupts,
    checkpoint_messages,
    clear_execution_persistence,
    execution_checkpoint_config,
)
from agent.runtime.container import RuntimeContainer
from api.chat import _public_stream_data
from core.config import get_settings
from core.security import AuthContext
from db.session import get_engine
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt
from memory.message_persister import MessagePersister
from models.base import utcnow
from models.chat import (
    AgentExecution,
    AgentInvocation,
    ChatMessage,
    ChatSession,
    ExecutionOutbox,
    ExecutionResumeRequest,
)
from models.enums import MessageRole, RunStatus
from models.schemas.chat import AgentMessageRequest, ChatRequest, UserReplyRequest
from pydantic import ValidationError
from services import tasks as tasks_module
from services.conversation_service import ConversationService
from services.errors import IdempotencyPayloadMismatchError, RunInterruptStaleError
from services.execution_claim import claim_execution
from services.execution_lineage import ExecutionLineage
from sqlalchemy import inspect, text
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


def test_runtime_config_uses_execution_scoped_checkpoint_namespace() -> None:
    runtime = _runtime_container().create_runtime(
        tool_permissions=(),
        user_id="user-1",
        agent_id="agent-1",
        session_id="thread-1",
        conversation_id="session-1",
        execution_id="execution-1",
    )

    configurable = runtime.config["configurable"]
    assert configurable["thread_id"] == "thread-1"
    assert configurable["checkpoint_ns"] == "execution-1"


def test_human_node_consumes_only_structured_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        graph_nodes,
        "interrupt",
        lambda _value: {"decision": "reject"},
    )
    node = graph_nodes.build_human_node()

    result = node({"messages": []})

    assert result["human_approved"] is False
    assert result["messages"][-1].content == "宸插彇娑堜繚瀛榒."


def test_public_interrupt_event_recursively_exposes_only_allowlisted_fields() -> None:
    projected = _public_stream_data(
        {
            "name": "run_interrupt",
            "interrupt": {
                "interrupt_id": "int-public",
                "tool_name": "remember",
                "purpose": "保存一条长期记忆",
                "memory": {"type": "preference", "content": "简洁回复"},
                "execution_id": "exe-secret",
                "task_id": "task-secret",
                "tool_call_id": "call-secret",
                "args": {"api_key": "secret"},
                "runtime": {"permissions": ["*"]},
            },
        },
        channel="interrupts",
    )

    assert projected == {
        "name": "run_interrupt",
        "interrupt": {
            "interrupt_id": "int-public",
            "tool_name": "remember",
            "purpose": "保存一条长期记忆",
            "memory": {"type": "preference", "content": "简洁回复"},
        },
    }


def test_invocation_request_digest_column_is_present_in_real_postgres() -> None:
    columns = {column["name"] for column in inspect(get_engine()).get_columns("agentinvocation")}

    assert "request_sha256" in columns


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
            created_at=now - timedelta(hours=1),
        )
        session.add(execution)
        session.flush()
        session.add(
            ExecutionOutbox(
                execution_id=execution.id,
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


def test_idempotency_key_is_bound_to_normalized_request_payload() -> None:
    service = ConversationService(SimpleNamespace())
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


def test_historical_key_without_digest_is_not_replayable() -> None:
    service = ConversationService(SimpleNamespace())
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
    service = ConversationService(SimpleNamespace())
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
        checkpoint_ns=old_execution_id,
    )

    try:
        list(graph.stream({"messages": []}, config=old_config))

        assert checkpoint_interrupts(
            checkpointer,
            thread_id=thread_id,
            checkpoint_ns=old_execution_id,
        )
        assert checkpoint_interrupts(
            checkpointer,
            thread_id=thread_id,
            checkpoint_ns=new_execution_id,
        ) == []
    finally:
        clear_execution_persistence(
            thread_id=thread_id,
            checkpoint_ns=old_execution_id,
            checkpointer=checkpointer,
        )
        clear_execution_persistence(
            thread_id=thread_id,
            checkpoint_ns=new_execution_id,
            checkpointer=checkpointer,
        )
        persistence.close()


def test_final_assistant_materialization_insert_or_reads_one_execution_message() -> None:
    class _Writer:
        def emit(self, _event: str, _payload: dict[str, Any]) -> None:
            raise AssertionError("message events must be emitted only after the terminal commit")

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
            event_writer=_Writer(),
        )
        second = persister.persist_assistant_text(
            session,
            session_id=chat.id,
            invocation_id=invocation.id,
            execution_id=execution.id,
            content="must not replace checkpoint final",
            event_writer=_Writer(),
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
    config = execution_checkpoint_config(thread_id=thread_id, checkpoint_ns=execution_id)

    try:
        list(graph.stream({"messages": []}, config=config))
        for _ in range(3):
            assert list(graph.stream(None, config=config)) == []
            recovered = checkpoint_messages(
                checkpointer,
                thread_id=thread_id,
                checkpoint_ns=execution_id,
            )
            assert recovered[-1].content == "durable final answer"
        assert side_effects == ["called"]
    finally:
        clear_execution_persistence(
            thread_id=thread_id,
            checkpoint_ns=execution_id,
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
            response, replayed = ConversationService(SimpleNamespace()).create_turn(
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
        )
        session.add(execution)
        session.flush()
        session.add(
            ExecutionOutbox(
                execution_id=execution.id,
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
