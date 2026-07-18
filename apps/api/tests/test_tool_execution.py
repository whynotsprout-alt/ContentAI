from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest
from agent.runtime.context import ToolRuntimeContext, tool_runtime_scope
from agent.runtime.tool_execution import execute_tool_call
from agent.tools.memory import remember
from db.session import get_engine
from langchain_core.messages import ToolMessage
from memory import LongTermMemory, MemoryRepository
from models.chat import AgentExecution, AgentInvocation, ChatSession, ToolExecution
from models.enums import RunStatus, ToolExecutionStatus
from models.memory import MemoryRecord
from pydantic import ValidationError
from sqlmodel import Session, select


def _seed_execution(execution_id: str) -> None:
    with Session(get_engine()) as session:
        chat = ChatSession(
            id=f"session-{execution_id}",
            agent_id="default-agent",
            agent_version_id="default-agent-v1",
            user_id="local-user",
        )
        session.add(chat)
        session.flush()
        invocation = AgentInvocation(
            id=f"invocation-{execution_id}",
            session_id=chat.id,
            agent_id="default-agent",
            user_id="local-user",
        )
        session.add(invocation)
        session.flush()
        session.add(
            AgentExecution(
                id=execution_id,
                invocation_id=invocation.id,
                session_id=chat.id,
                agent_version_id="default-agent-v1",
                status=RunStatus.running,
            )
        )
        session.commit()


def _runtime(
    execution_id: str,
    policies: dict[str, dict[str, object]],
    *,
    event_writer: object | None = None,
) -> ToolRuntimeContext:
    return ToolRuntimeContext(
        execution_id=execution_id,
        conversation_id=f"session-{execution_id}",
        session_id=f"session-{execution_id}",
        agent_id="default-agent",
        user_id="local-user",
        tool_policies=policies,
        event_writer=event_writer,
    )


class RecordingEventWriter:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def emit(self, event: str, payload: dict[str, object]) -> None:
        self.events.append((event, payload))


@pytest.mark.parametrize(
    ("sensitive_content", "forbidden_fragment"),
    [
        ("my API     key is never-log-this-value", "never-log-this-value"),
        ("OPENAI_API_KEY=opaque-api-tool", "opaque-api-tool"),
        (
            "google_client_secret = opaque-client-tool",
            "opaque-client-tool",
        ),
        ("GitHub-Access-Token: opaque-access-tool", "opaque-access-tool"),
        ("RSA PRIVATE KEY = opaque-private-tool", "opaque-private-tool"),
        ("googleClientSecret=opaque-camel-tool", "opaque-camel-tool"),
    ],
)
def test_remember_rejects_sensitive_labels_without_persisting_or_logging(
    sensitive_content: str,
    forbidden_fragment: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    execution_id = "execution-sensitive-remember"
    _seed_execution(execution_id)
    with Session(get_engine()) as session:
        runtime = _runtime(execution_id, {})
        runtime.long_term_memory = LongTermMemory(MemoryRepository(session))
        with tool_runtime_scope(runtime):
            result = remember.invoke(
                {"content": sensitive_content, "kind": "preference"}
            )
        persisted = session.exec(
            select(MemoryRecord).where(MemoryRecord.content == sensitive_content)
        ).all()

    assert result == {
        "error": "Potentially sensitive content is not allowed for memory storage.",
        "tool": "remember",
    }
    assert persisted == []
    assert forbidden_fragment not in caplog.text


def test_remember_rejects_bytes_kind_before_execution_without_persisting() -> None:
    execution_id = "execution-bytes-kind"
    content = "must not persist bytes kind"
    _seed_execution(execution_id)
    with Session(get_engine()) as session:
        runtime = _runtime(execution_id, {})
        runtime.long_term_memory = LongTermMemory(MemoryRepository(session))
        with tool_runtime_scope(runtime):
            with pytest.raises(ValidationError):
                remember.invoke({"content": content, "kind": b"preference"})
        persisted = session.exec(
            select(MemoryRecord).where(MemoryRecord.content == content)
        ).all()

    assert persisted == []


def test_tool_output_is_bounded_and_audit_does_not_store_content() -> None:
    execution_id = "execution-tool-output"
    _seed_execution(execution_id)
    request = SimpleNamespace(
        tool_call={"name": "large_result", "id": "call-large", "args": {"secret": "value"}}
    )

    with tool_runtime_scope(
        _runtime(
            execution_id,
            {"large_result": {"timeout_seconds": 1, "max_output_chars": 32}},
        )
    ):
        result = execute_tool_call(request, lambda _request: {"payload": "x" * 200})

    assert isinstance(result, ToolMessage)
    assert "truncated" in str(result.content)
    with Session(get_engine()) as session:
        audit = session.exec(select(ToolExecution)).one()
        assert audit.status == ToolExecutionStatus.completed
        assert audit.arguments_hash
        assert audit.result_digest
        assert "secret" not in audit.error


def test_tool_execution_emits_start_and_end_progress_events() -> None:
    execution_id = "execution-tool-events"
    _seed_execution(execution_id)
    writer = RecordingEventWriter()
    request = SimpleNamespace(
        tool_call={"name": "fetch_hotspots", "id": "call-events", "args": {}}
    )

    with tool_runtime_scope(
        _runtime(
            execution_id,
            {"fetch_hotspots": {"timeout_seconds": 1, "max_output_chars": 100}},
            event_writer=writer,
        )
    ):
        result = execute_tool_call(request, lambda _request: {"ok": True})

    assert result == {"ok": True}
    assert [event for event, _ in writer.events] == ["tool_start", "tool_end"]
    assert writer.events[0][1]["tool_name"] == "fetch_hotspots"
    assert writer.events[1][1]["status"] == "completed"


def test_tool_timeout_returns_structured_error_and_marks_audit_failed() -> None:
    execution_id = "execution-tool-timeout"
    _seed_execution(execution_id)
    request = SimpleNamespace(
        tool_call={"name": "slow_tool", "id": "call-slow", "args": {}}
    )

    with tool_runtime_scope(
        _runtime(
            execution_id,
            {"slow_tool": {"timeout_seconds": 0.005, "max_output_chars": 100}},
        )
    ):
        result = execute_tool_call(
            request,
            lambda _request: (time.sleep(0.05), "late result")[1],
        )

    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert "TOOL_TIMEOUT" in str(result.content)
    with Session(get_engine()) as session:
        audit = session.exec(select(ToolExecution)).one()
        assert audit.status == ToolExecutionStatus.failed
        assert audit.result_digest == ""
        assert "TOOL_TIMEOUT" in audit.error


def test_side_effecting_tool_call_is_idempotent_per_execution_and_call_id() -> None:
    execution_id = "execution-tool-idempotent"
    _seed_execution(execution_id)
    request = SimpleNamespace(
        tool_call={"name": "side_effect", "id": "call-once", "args": {"value": 1}}
    )
    calls = 0

    def execute(_request: object) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        return {"completed": True}

    with tool_runtime_scope(
        _runtime(
            execution_id,
            {
                "side_effect": {
                    "timeout_seconds": 1,
                    "max_output_chars": 100,
                    "side_effecting": True,
                }
            },
        )
    ):
        first = execute_tool_call(request, execute)
        second = execute_tool_call(request, execute)

    assert first == {"completed": True}
    assert isinstance(second, ToolMessage)
    assert "already completed" in str(second.content)
    assert calls == 1
    with Session(get_engine()) as session:
        audits = session.exec(select(ToolExecution)).all()
        assert len(audits) == 1
        assert audits[0].status == ToolExecutionStatus.completed
