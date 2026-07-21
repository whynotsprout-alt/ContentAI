from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest
from agent.runtime.context import ToolRuntimeContext, tool_runtime_scope
from agent.runtime.tool_execution import execute_tool_call
from agent.tools.memory import remember
from agent.workflows.deep_research import ContentEvidenceInvalidError
from db.session import get_engine
from langchain_core.messages import ToolMessage
from memory import LongTermMemory, MemoryRepository
from model_config_helpers import DEFAULT_MODEL_CONFIG_ID
from models.base import utcnow
from models.chat import (
    AgentExecution,
    AgentInvocation,
    ChatSession,
    ToolExecution,
)
from models.enums import RunStatus, ToolExecutionStatus
from models.memory import MemoryRecord
from pydantic import ValidationError
from services.execution_resume import stable_json_hash
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
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
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


def test_public_terminal_tool_error_propagates_to_execution_runner() -> None:
    execution_id = "execution-terminal-tool-error"
    _seed_execution(execution_id)
    request = SimpleNamespace(
        tool_call={"name": "prepare_topic_research", "id": "call-evidence", "args": {}}
    )

    with tool_runtime_scope(
        _runtime(
            execution_id,
            {"prepare_topic_research": {"timeout_seconds": 1, "max_output_chars": 100}},
        )
    ), pytest.raises(ContentEvidenceInvalidError):
        execute_tool_call(
            request,
            lambda _request: (_ for _ in ()).throw(ContentEvidenceInvalidError()),
        )


def test_tool_provider_error_is_redacted_from_message_event_and_audit() -> None:
    execution_id = "execution-redacted-tool-error"
    _seed_execution(execution_id)
    writer = RecordingEventWriter()
    request = SimpleNamespace(
        tool_call={"name": "prepare_topic_research", "id": "call-redacted", "args": {}}
    )
    secret = "sk-tool-provider-secret"
    remote_body = "provider response body must stay private"

    def fail(_request: object) -> object:
        raise RuntimeError(f"401 Authorization: Bearer {secret}; body={remote_body}")

    with tool_runtime_scope(
        _runtime(
            execution_id,
            {
                "prepare_topic_research": {
                    "timeout_seconds": 1,
                    "max_output_chars": 1000,
                    "execution_mode": "cooperative",
                }
            },
            event_writer=writer,
        )
    ):
        result = execute_tool_call(request, fail)

    with Session(get_engine()) as session:
        audit = session.exec(select(ToolExecution)).one()

    exposed = " ".join(
        [
            str(result.content),
            audit.error,
            *(str(payload) for _event, payload in writer.events),
        ]
    )
    assert secret not in exposed
    assert remote_body not in exposed
    assert "Authorization" not in exposed


def test_side_effecting_tool_call_is_idempotent_per_execution_and_call_id() -> None:
    execution_id = "execution-tool-idempotent"
    _seed_execution(execution_id)
    request = SimpleNamespace(
        tool_call={"name": "side_effect", "id": "call-once", "args": {"value": 1}}
    )
    calls = 0
    dispatched: list[dict[str, object]] = []

    def execute(_request: object) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        return {"completed": True}

    runtime = _runtime(
        execution_id,
        {
            "side_effect": {
                "timeout_seconds": 1,
                "max_output_chars": 100,
                "side_effecting": True,
            }
        },
    )
    runtime.side_effect_dispatcher = dispatched.append
    runtime.side_effect_receipt_poller = lambda *_identity: (
        {
            "status": "completed",
            "result": {"completed": True},
            "result_digest": "completed-digest",
        }
        if dispatched
        else None
    )
    with tool_runtime_scope(runtime):
        first = execute_tool_call(request, execute)
        second = execute_tool_call(request, execute)

    assert isinstance(first, ToolMessage)
    assert isinstance(second, ToolMessage)
    assert "completed" in str(first.content)
    assert "completed" in str(second.content)
    assert calls == 0
    assert len(dispatched) == 1
    with Session(get_engine()) as session:
        audits = session.exec(select(ToolExecution)).all()
        assert len(audits) == 1
        assert audits[0].status == ToolExecutionStatus.completed


def test_side_effecting_tool_is_dispatched_without_invoking_local_callback() -> None:
    execution_id = "execution-side-effect-dispatch"
    _seed_execution(execution_id)
    request = SimpleNamespace(
        tool_call={"name": "remember", "id": "call-dispatch", "args": {"content": "x"}}
    )
    calls = 0
    dispatched: list[dict[str, object]] = []

    def execute(_request: object) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        return {"completed": True}

    runtime = _runtime(
        execution_id,
        {
            "remember": {
                "version": "1",
                "timeout_seconds": 0.05,
                "max_output_chars": 100,
                "side_effecting": True,
            }
        },
    )
    runtime.side_effect_dispatcher = dispatched.append
    runtime.side_effect_receipt_poller = lambda *_identity: (
        {
            "status": "completed",
            "result": {"completed": True},
            "result_digest": "receipt-digest",
        }
        if dispatched
        else None
    )

    with tool_runtime_scope(runtime):
        result = execute_tool_call(request, execute)

    assert isinstance(result, ToolMessage)
    assert "completed" in str(result.content)
    assert calls == 0
    assert len(dispatched) == 1
    assert dispatched[0]["execution_id"] == execution_id
    assert dispatched[0]["tool_call_id"] == "call-dispatch"


def test_non_side_effecting_tool_stays_on_local_execution_path() -> None:
    execution_id = "execution-local-tool"
    _seed_execution(execution_id)
    request = SimpleNamespace(
        tool_call={"name": "local_tool", "id": "call-local", "args": {}}
    )
    calls = 0

    def execute(_request: object) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        return {"completed": True}

    with tool_runtime_scope(
        _runtime(
            execution_id,
            {"local_tool": {"timeout_seconds": 1, "max_output_chars": 100}},
        )
    ):
        result = execute_tool_call(request, execute)

    assert result == {"completed": True}
    assert calls == 1


def test_side_effect_retry_rejects_changed_arguments_hash_without_redispatch() -> None:
    execution_id = "execution-side-effect-mismatch"
    _seed_execution(execution_id)
    dispatched: list[dict[str, object]] = []
    runtime = _runtime(
        execution_id,
        {
            "remember": {
                "version": "1",
                "timeout_seconds": 0.05,
                "max_output_chars": 100,
                "side_effecting": True,
            }
        },
    )
    runtime.side_effect_dispatcher = dispatched.append
    runtime.side_effect_receipt_poller = lambda *_identity: (
        {
            "status": "completed",
            "result": {"key": "memory-key", "kind": "preference", "tool": "remember"},
            "result_digest": "receipt-digest",
        }
        if dispatched
        else None
    )

    first = SimpleNamespace(
        tool_call={"name": "remember", "id": "call-mismatch", "args": {"content": "first"}}
    )
    changed = SimpleNamespace(
        tool_call={"name": "remember", "id": "call-mismatch", "args": {"content": "changed"}}
    )
    with tool_runtime_scope(runtime):
        initial = execute_tool_call(first, lambda _request: None)
        result = execute_tool_call(changed, lambda _request: None)

    assert isinstance(initial, ToolMessage)
    assert "memory-key" in str(initial.content)
    assert isinstance(result, ToolMessage)
    assert "SIDE_EFFECT_IDEMPOTENCY_MISMATCH" in str(result.content)
    assert len(dispatched) == 1


def test_side_effect_timeout_does_not_redispatch_and_retry_reads_late_receipt() -> None:
    execution_id = "execution-side-effect-late-receipt"
    _seed_execution(execution_id)
    dispatched: list[dict[str, object]] = []
    receipt: dict[str, object] | None = None
    runtime = _runtime(
        execution_id,
        {
            "remember": {
                "version": "1",
                "timeout_seconds": 0.005,
                "max_output_chars": 100,
                "side_effecting": True,
            }
        },
    )
    runtime.side_effect_dispatcher = dispatched.append
    runtime.side_effect_receipt_poller = lambda *_identity: receipt
    request = SimpleNamespace(
        tool_call={"name": "remember", "id": "call-late", "args": {"content": "later"}}
    )

    with tool_runtime_scope(runtime):
        timed_out = execute_tool_call(request, lambda _request: None)
        with Session(get_engine()) as session:
            audit = session.exec(select(ToolExecution)).one()
            audit.status = ToolExecutionStatus.completed
            audit.result_digest = "late-digest"
            audit.finished_at = utcnow()
            session.add(audit)
            session.commit()
        receipt = {
            "status": "completed",
            "result": {"key": "late-key", "kind": "semantic", "tool": "remember"},
            "result_digest": "late-digest",
        }
        retried = execute_tool_call(request, lambda _request: None)

    assert isinstance(timed_out, ToolMessage)
    assert "TOOL_TIMEOUT" in str(timed_out.content)
    assert isinstance(retried, ToolMessage)
    assert "late-key" in str(retried.content)
    assert len(dispatched) == 1
    with Session(get_engine()) as session:
        audit = session.exec(select(ToolExecution)).one()
        assert audit.status == ToolExecutionStatus.completed
        assert audit.result_digest == "late-digest"


def test_side_effect_timeout_does_not_wait_for_worker_row_lock() -> None:
    execution_id = "execution-side-effect-row-lock-timeout"
    _seed_execution(execution_id)
    worker_thread: threading.Thread | None = None
    lock_acquired = threading.Event()

    def hold_audit_lock() -> None:
        with Session(get_engine()) as session:
            session.exec(
                select(ToolExecution)
                .where(ToolExecution.execution_id == execution_id)
                .with_for_update()
            ).one()
            lock_acquired.set()
            time.sleep(0.25)

    def dispatch(_job: dict[str, object]) -> None:
        nonlocal worker_thread
        worker_thread = threading.Thread(target=hold_audit_lock)
        worker_thread.start()
        assert lock_acquired.wait(timeout=1)

    runtime = _runtime(
        execution_id,
        {
            "remember": {
                "version": "1",
                "timeout_seconds": 0.005,
                "max_output_chars": 100,
                "side_effecting": True,
            }
        },
    )
    runtime.side_effect_dispatcher = dispatch
    runtime.side_effect_receipt_poller = lambda *_identity: None
    request = SimpleNamespace(
        tool_call={"name": "remember", "id": "call-row-lock", "args": {"content": "x"}}
    )

    started_at = time.perf_counter()
    with tool_runtime_scope(runtime):
        result = execute_tool_call(request, lambda _request: None)
    elapsed = time.perf_counter() - started_at
    assert worker_thread is not None
    worker_thread.join(timeout=1)

    assert isinstance(result, ToolMessage)
    assert "TOOL_TIMEOUT" in str(result.content)
    assert elapsed < 0.1
    with Session(get_engine()) as session:
        audit = session.exec(select(ToolExecution)).one()
        assert audit.status == ToolExecutionStatus.running
        assert audit.error == ""


def test_side_effect_dispatch_emits_one_start_and_terminal_event() -> None:
    execution_id = "execution-side-effect-events"
    _seed_execution(execution_id)
    writer = RecordingEventWriter()
    dispatched: list[dict[str, object]] = []
    runtime = _runtime(
        execution_id,
        {
            "remember": {
                "version": "1",
                "timeout_seconds": 0.05,
                "max_output_chars": 100,
                "side_effecting": True,
            }
        },
        event_writer=writer,
    )
    runtime.side_effect_dispatcher = dispatched.append
    runtime.side_effect_receipt_poller = lambda *_identity: (
        {
            "status": "completed",
            "result": {"key": "event-key", "kind": "semantic", "tool": "remember"},
            "result_digest": "event-digest",
        }
        if dispatched
        else None
    )
    request = SimpleNamespace(
        tool_call={"name": "remember", "id": "call-event", "args": {"content": "x"}}
    )

    with tool_runtime_scope(runtime):
        execute_tool_call(request, lambda _request: None)

    assert [name for name, _payload in writer.events] == ["tool_start", "tool_end"]
    assert writer.events[-1][1]["status"] == "completed"


def test_side_effect_timeout_bounds_blocking_dispatcher() -> None:
    execution_id = "execution-side-effect-blocking-dispatch"
    _seed_execution(execution_id)
    dispatcher_entered = threading.Event()
    dispatcher_release = threading.Event()
    local_calls = 0

    def blocking_dispatcher(_job: dict[str, object]) -> None:
        dispatcher_entered.set()
        dispatcher_release.wait(timeout=0.25)

    def local_callback(_request: object) -> None:
        nonlocal local_calls
        local_calls += 1

    runtime = _runtime(
        execution_id,
        {
            "remember": {
                "version": "1",
                "timeout_seconds": 0.01,
                "max_output_chars": 100,
                "side_effecting": True,
            }
        },
    )
    runtime.side_effect_dispatcher = blocking_dispatcher
    runtime.side_effect_receipt_poller = lambda *_identity: None
    request = SimpleNamespace(
        tool_call={"name": "remember", "id": "call-blocking-dispatch", "args": {}}
    )

    started_at = time.perf_counter()
    with tool_runtime_scope(runtime):
        result = execute_tool_call(request, local_callback)
    elapsed = time.perf_counter() - started_at
    dispatcher_release.set()

    assert dispatcher_entered.is_set()
    assert isinstance(result, ToolMessage)
    assert "TOOL_TIMEOUT" in str(result.content)
    assert elapsed < 0.1
    assert local_calls == 0


def test_side_effect_timeout_bounds_blocking_initial_receipt_poll() -> None:
    execution_id = "execution-side-effect-blocking-poll"
    _seed_execution(execution_id)
    poller_entered = threading.Event()
    poller_release = threading.Event()
    dispatched: list[dict[str, object]] = []
    local_calls = 0

    def blocking_poller(_execution_id: str, _tool_call_id: str) -> None:
        poller_entered.set()
        poller_release.wait(timeout=0.25)
        return None

    def local_callback(_request: object) -> None:
        nonlocal local_calls
        local_calls += 1

    runtime = _runtime(
        execution_id,
        {
            "remember": {
                "version": "1",
                "timeout_seconds": 0.01,
                "max_output_chars": 100,
                "side_effecting": True,
            }
        },
    )
    runtime.side_effect_dispatcher = dispatched.append
    runtime.side_effect_receipt_poller = blocking_poller
    request = SimpleNamespace(
        tool_call={"name": "remember", "id": "call-blocking-poll", "args": {}}
    )

    started_at = time.perf_counter()
    with tool_runtime_scope(runtime):
        result = execute_tool_call(request, local_callback)
    elapsed = time.perf_counter() - started_at
    poller_release.set()

    assert poller_entered.is_set()
    assert isinstance(result, ToolMessage)
    assert "TOOL_TIMEOUT" in str(result.content)
    assert elapsed < 0.1
    assert dispatched == []
    assert local_calls == 0


def test_terminal_receipt_retry_preserves_original_audit_timestamps() -> None:
    execution_id = "execution-side-effect-terminal-timestamps"
    arguments = {"content": "already committed", "kind": "preference"}
    arguments_hash = stable_json_hash(arguments)
    _seed_execution(execution_id)
    original_started_at = utcnow() - timedelta(seconds=10)
    original_finished_at = utcnow() - timedelta(seconds=5)
    with Session(get_engine()) as session:
        session.add(
            ToolExecution(
                execution_id=execution_id,
                tool_name="remember",
                tool_version="1",
                tool_call_id="call-terminal-timestamps",
                arguments_hash=arguments_hash,
                result_digest="original-digest",
                status=ToolExecutionStatus.completed,
                started_at=original_started_at,
                finished_at=original_finished_at,
                duration_ms=5000,
            )
        )
        session.commit()
    runtime = _runtime(
        execution_id,
        {
            "remember": {
                "version": "1",
                "timeout_seconds": 0.05,
                "max_output_chars": 100,
                "side_effecting": True,
            }
        },
    )
    runtime.side_effect_dispatcher = lambda _job: (_ for _ in ()).throw(
        AssertionError("terminal retry must not dispatch")
    )
    runtime.side_effect_receipt_poller = lambda *_identity: {
        "status": "completed",
        "result": {"key": "committed-key", "kind": "preference", "tool": "remember"},
        "result_digest": "original-digest",
    }
    request = SimpleNamespace(
        tool_call={
            "name": "remember",
            "id": "call-terminal-timestamps",
            "args": arguments,
        }
    )

    with tool_runtime_scope(runtime):
        result = execute_tool_call(request, lambda _request: None)

    assert isinstance(result, ToolMessage)
    assert "committed-key" in str(result.content)
    with Session(get_engine()) as session:
        audit = session.exec(select(ToolExecution)).one()
        assert audit.started_at == original_started_at
        assert audit.finished_at == original_finished_at
        assert audit.duration_ms == 5000
        assert audit.result_digest == "original-digest"


def test_side_effect_remote_io_capacity_stays_bounded_when_calls_block() -> None:
    call_count = 10
    execution_ids = [f"execution-side-effect-capacity-{index}" for index in range(call_count)]
    for execution_id in execution_ids:
        _seed_execution(execution_id)
    release_calls = threading.Event()
    call_lock = threading.Lock()
    started_calls = 0
    active_calls = 0
    max_active_calls = 0
    dispatched: list[dict[str, object]] = []
    local_calls = 0

    def blocking_poller(_execution_id: str, _tool_call_id: str) -> None:
        nonlocal active_calls, max_active_calls, started_calls
        with call_lock:
            started_calls += 1
            active_calls += 1
            max_active_calls = max(max_active_calls, active_calls)
        try:
            release_calls.wait(timeout=1)
        finally:
            with call_lock:
                active_calls -= 1
        return None

    def invoke(index: int) -> ToolMessage:
        nonlocal local_calls
        runtime = _runtime(
            execution_ids[index],
            {
                "remember": {
                    "version": "1",
                    "timeout_seconds": 0.03,
                    "max_output_chars": 100,
                    "side_effecting": True,
                }
            },
        )
        runtime.side_effect_dispatcher = dispatched.append
        runtime.side_effect_receipt_poller = blocking_poller
        request = SimpleNamespace(
            tool_call={
                "name": "remember",
                "id": f"call-capacity-{index}",
                "args": {},
            }
        )

        def local_callback(_request: object) -> None:
            nonlocal local_calls
            with call_lock:
                local_calls += 1

        with tool_runtime_scope(runtime):
            result = execute_tool_call(request, local_callback)
        assert isinstance(result, ToolMessage)
        return result

    with ThreadPoolExecutor(max_workers=call_count) as callers:
        results = list(callers.map(invoke, range(call_count)))

    with call_lock:
        started_before_release = started_calls
        max_active_before_release = max_active_calls
    remote_threads_before_release = sum(
        thread.name.startswith("side-effect-io") for thread in threading.enumerate()
    )
    release_calls.set()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with call_lock:
            if active_calls == 0:
                break
        time.sleep(0.01)

    assert all("TOOL_TIMEOUT" in str(result.content) for result in results)
    assert started_before_release <= 4
    assert max_active_before_release <= 4
    assert remote_threads_before_release <= 4
    assert dispatched == []
    assert local_calls == 0
