from __future__ import annotations

import importlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from agent.runtime.context import ToolRuntimeContext, tool_runtime_scope
from agent.runtime.tool_execution import execute_tool_call
from db.session import get_engine
from langchain_core.messages import ToolMessage
from models.base import utcnow
from models.chat import ChatSession, SideEffectReceipt, ToolExecution
from models.enums import ToolExecutionStatus
from models.memory import MemoryRecord
from services.execution_resume import stable_json_hash
from sqlmodel import Session, select
from test_tool_execution import _seed_execution


def _seed_audit(execution_id: str, *, arguments_hash: str = "arguments-hash") -> None:
    _seed_execution(execution_id)
    with Session(get_engine()) as session:
        session.add(
            ToolExecution(
                execution_id=execution_id,
                tool_name="remember",
                tool_version="1",
                tool_call_id="call-remember",
                arguments_hash=arguments_hash,
                status=ToolExecutionStatus.running,
            )
        )
        session.commit()


def _job(execution_id: str, *, content: str = "durable preference") -> dict[str, Any]:
    with Session(get_engine()) as session:
        chat = session.get(ChatSession, f"session-{execution_id}")
        assert chat is not None
        thread_id = chat.langgraph_thread_id
    return {
        "execution_id": execution_id,
        "tool_call_id": "call-remember",
        "tool_name": "remember",
        "tool_version": "1",
        "arguments": {"content": content, "kind": "preference"},
        "arguments_hash": "arguments-hash",
        "user_id": "local-user",
        "agent_id": "default-agent",
        "agent_version_id": "default-agent-v1",
        "session_id": f"session-{execution_id}",
        "thread_id": thread_id,
    }


def test_remember_mutation_and_completed_receipt_commit_together() -> None:
    execution_id = "execution-side-effect-transaction"
    _seed_audit(execution_id)
    side_effects = importlib.import_module("services.side_effects")

    outcome = side_effects.execute_side_effect_job(_job(execution_id))

    assert outcome["status"] == "completed"
    with Session(get_engine()) as session:
        memory = session.exec(
            select(MemoryRecord).where(MemoryRecord.content == "durable preference")
        ).one()
        receipt = session.exec(select(SideEffectReceipt)).one()
        audit = session.exec(select(ToolExecution)).one()
        assert memory.source_execution_id == execution_id
        assert receipt.status == "completed"
        assert receipt.result_digest
        assert receipt.detail["result"] == {
            "key": memory.memory_key,
            "kind": "preference",
            "tool": "remember",
        }
        assert audit.status == ToolExecutionStatus.completed
        assert audit.result_digest == receipt.result_digest


def test_exception_before_commit_rolls_back_mutation_and_writes_safe_failure() -> None:
    execution_id = "execution-side-effect-rollback"
    _seed_audit(execution_id)
    side_effects = importlib.import_module("services.side_effects")

    def crash_after_mutation(session: Session, _job_payload: dict[str, Any]) -> dict[str, str]:
        session.add(
            MemoryRecord(
                user_id="local-user",
                agent_id="default-agent",
                memory_key="must-rollback",
                content="must rollback",
            )
        )
        session.flush()
        raise RuntimeError("opaque-secret-must-not-persist")

    outcome = side_effects.execute_side_effect_job(
        _job(execution_id),
        operation_runner=crash_after_mutation,
    )

    assert outcome == {"status": "failed", "error": "SIDE_EFFECT_EXECUTION_FAILED"}
    with Session(get_engine()) as session:
        assert session.exec(select(MemoryRecord)).all() == []
        receipt = session.exec(select(SideEffectReceipt)).one()
        audit = session.exec(select(ToolExecution)).one()
        assert receipt.status == "failed"
        assert receipt.detail["error"] == "SIDE_EFFECT_EXECUTION_FAILED"
        assert "opaque-secret" not in str(receipt.detail)
        assert audit.status == ToolExecutionStatus.failed
        assert audit.error == "SIDE_EFFECT_EXECUTION_FAILED"


def test_default_dispatcher_routes_stable_job_to_side_effect_worker(
    monkeypatch: Any,
) -> None:
    execution_id = "execution-side-effect-default-dispatch"
    _seed_execution(execution_id)
    with Session(get_engine()) as session:
        chat = session.get(ChatSession, f"session-{execution_id}")
        assert chat is not None
        thread_id = chat.langgraph_thread_id
    dispatched: list[dict[str, Any]] = []

    def send_task(
        name: str,
        *,
        kwargs: dict[str, Any],
        queue: str,
        task_id: str,
    ) -> None:
        dispatched.append(
            {"name": name, "kwargs": kwargs, "queue": queue, "task_id": task_id}
        )
        side_effects = importlib.import_module("services.side_effects")
        side_effects.execute_side_effect_job(kwargs["job"])

    celery_module = importlib.import_module("services.celery_app")
    monkeypatch.setattr(celery_module.celery_app, "send_task", send_task)
    local_calls = 0

    def local_callback(_request: object) -> None:
        nonlocal local_calls
        local_calls += 1

    runtime = ToolRuntimeContext(
        execution_id=execution_id,
        conversation_id=f"session-{execution_id}",
        session_id=thread_id,
        agent_id="default-agent",
        agent_version_id="default-agent-v1",
        user_id="local-user",
        permissions=["remember"],
        tool_policies={
            "remember": {
                "version": "1",
                "timeout_seconds": 0.1,
                "max_output_chars": 100,
                "side_effecting": True,
            }
        },
    )
    request = SimpleNamespace(
        tool_call={
            "name": "remember",
            "id": "call-default-dispatch",
            "args": {"content": "default dispatch memory", "kind": "preference"},
        }
    )

    with tool_runtime_scope(runtime):
        result = execute_tool_call(request, local_callback)

    assert "remember" in str(result.content)
    assert local_calls == 0
    assert len(dispatched) == 1
    assert dispatched[0]["name"] == "contentai.execute_side_effect"
    assert dispatched[0]["queue"] == "agent-side-effects"
    assert dispatched[0]["task_id"].endswith(
        f"{execution_id}:call-default-dispatch"
    )


def test_duplicate_deliveries_commit_one_mutation_and_return_one_receipt() -> None:
    execution_id = "execution-side-effect-duplicate"
    _seed_audit(execution_id)
    side_effects = importlib.import_module("services.side_effects")
    runner_calls = 0
    runner_lock = threading.Lock()
    start = threading.Barrier(2)

    def operation_runner(session: Session, _job_payload: dict[str, Any]) -> dict[str, str]:
        nonlocal runner_calls
        with runner_lock:
            runner_calls += 1
        row = MemoryRecord(
            user_id="local-user",
            agent_id="default-agent",
            memory_key="duplicate-once",
            content="duplicate once",
        )
        session.add(row)
        session.flush()
        return {"key": row.memory_key, "kind": "semantic", "tool": "remember"}

    def deliver() -> dict[str, Any]:
        start.wait()
        return side_effects.execute_side_effect_job(
            _job(execution_id),
            operation_runner=operation_runner,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: deliver(), range(2)))

    assert [outcome["status"] for outcome in outcomes] == ["completed", "completed"]
    assert runner_calls == 1
    with Session(get_engine()) as session:
        assert len(session.exec(select(MemoryRecord)).all()) == 1
        assert len(session.exec(select(SideEffectReceipt)).all()) == 1


def test_worker_rejects_changed_arguments_hash_without_mutation() -> None:
    execution_id = "execution-side-effect-worker-mismatch"
    _seed_audit(execution_id)
    side_effects = importlib.import_module("services.side_effects")
    calls = 0

    def operation_runner(_session: Session, _job_payload: dict[str, Any]) -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"key": "unexpected"}

    changed = _job(execution_id)
    changed["arguments_hash"] = "changed-hash"
    outcome = side_effects.execute_side_effect_job(
        changed,
        operation_runner=operation_runner,
    )

    assert outcome == {
        "status": "failed",
        "error": "SIDE_EFFECT_IDEMPOTENCY_MISMATCH",
    }
    assert calls == 0
    with Session(get_engine()) as session:
        assert session.exec(select(SideEffectReceipt)).all() == []


def test_sensitive_remember_failure_does_not_persist_secret_in_receipt_or_log(
    caplog: Any,
) -> None:
    execution_id = "execution-side-effect-sensitive"
    _seed_audit(execution_id)
    side_effects = importlib.import_module("services.side_effects")
    secret = "OPENAI_API_KEY=opaque-side-effect-secret"

    outcome = side_effects.execute_side_effect_job(_job(execution_id, content=secret))

    assert outcome == {"status": "failed", "error": "SIDE_EFFECT_EXECUTION_FAILED"}
    with Session(get_engine()) as session:
        receipt = session.exec(select(SideEffectReceipt)).one()
        assert len(str(receipt.detail)) < 1000
        assert "opaque-side-effect-secret" not in str(receipt.detail)
    assert "opaque-side-effect-secret" not in caplog.text


def test_stale_reconciler_converges_from_receipts_and_marks_unknown_without_replay(
    monkeypatch: Any,
) -> None:
    side_effects = importlib.import_module("services.side_effects")
    failed_execution = "execution-reconcile-failed"
    completed_execution = "execution-reconcile-completed"
    unknown_execution = "execution-reconcile-unknown"
    for execution_id in (failed_execution, completed_execution, unknown_execution):
        _seed_audit(execution_id)
    stale_at = utcnow() - timedelta(minutes=10)
    with Session(get_engine()) as session:
        for audit in session.exec(select(ToolExecution)).all():
            audit.updated_at = stale_at
            session.add(audit)
        session.add(
            SideEffectReceipt(
                execution_id=failed_execution,
                tool_call_id="call-remember",
                operation="remember",
                status="failed",
                detail={"error": "SIDE_EFFECT_EXECUTION_FAILED"},
            )
        )
        session.add(
            SideEffectReceipt(
                execution_id=completed_execution,
                tool_call_id="call-remember",
                operation="remember",
                status="completed",
                result_digest="completed-digest",
                detail={"result": {"key": "done", "kind": "semantic", "tool": "remember"}},
            )
        )
        session.commit()

    monkeypatch.setattr(
        side_effects,
        "dispatch_side_effect_job",
        lambda _job_payload: (_ for _ in ()).throw(AssertionError("must not replay")),
    )
    reconciled = side_effects.reconcile_stale_side_effects(
        older_than_seconds=60,
        limit=10,
    )

    assert reconciled == 3
    with Session(get_engine()) as session:
        audits = {
            audit.execution_id: audit for audit in session.exec(select(ToolExecution)).all()
        }
        assert audits[failed_execution].status == ToolExecutionStatus.failed
        assert audits[failed_execution].error == "SIDE_EFFECT_EXECUTION_FAILED"
        assert audits[completed_execution].status == ToolExecutionStatus.completed
        assert audits[completed_execution].result_digest == "completed-digest"
        assert audits[unknown_execution].status == ToolExecutionStatus.failed
        assert audits[unknown_execution].error == "SIDE_EFFECT_OUTCOME_UNKNOWN"


def test_stale_reconciler_is_bounded() -> None:
    side_effects = importlib.import_module("services.side_effects")
    for index in range(3):
        _seed_audit(f"execution-reconcile-bounded-{index}")
    with Session(get_engine()) as session:
        for audit in session.exec(select(ToolExecution)).all():
            audit.updated_at = utcnow() - timedelta(minutes=10)
            session.add(audit)
        session.commit()

    assert side_effects.reconcile_stale_side_effects(older_than_seconds=60, limit=2) == 2
    with Session(get_engine()) as session:
        running = session.exec(
            select(ToolExecution).where(ToolExecution.status == ToolExecutionStatus.running)
        ).all()
        assert len(running) == 1


def test_unknown_outcome_fences_delayed_delivery_and_agent_retry() -> None:
    execution_id = "execution-reconcile-fenced-unknown"
    arguments = {"content": "durable preference", "kind": "preference"}
    arguments_hash = stable_json_hash(arguments)
    _seed_audit(execution_id, arguments_hash=arguments_hash)
    side_effects = importlib.import_module("services.side_effects")
    with Session(get_engine()) as session:
        audit = session.exec(select(ToolExecution)).one()
        audit.updated_at = utcnow() - timedelta(minutes=10)
        session.add(audit)
        session.commit()
    assert side_effects.reconcile_stale_side_effects(older_than_seconds=60) == 1
    runner_calls = 0

    def operation_runner(_session: Session, _job_payload: dict[str, Any]) -> dict[str, str]:
        nonlocal runner_calls
        runner_calls += 1
        return {"key": "must-not-run"}

    delayed_job = _job(execution_id)
    delayed_job["arguments_hash"] = arguments_hash
    delayed = side_effects.execute_side_effect_job(
        delayed_job,
        operation_runner=operation_runner,
    )
    dispatched: list[dict[str, object]] = []
    runtime = ToolRuntimeContext(
        execution_id=execution_id,
        conversation_id=f"session-{execution_id}",
        session_id=_job(execution_id)["thread_id"],
        agent_id="default-agent",
        agent_version_id="default-agent-v1",
        user_id="local-user",
        permissions=["remember"],
        tool_policies={
            "remember": {
                "version": "1",
                "timeout_seconds": 0.005,
                "max_output_chars": 100,
                "side_effecting": True,
            }
        },
        side_effect_dispatcher=dispatched.append,
        side_effect_receipt_poller=lambda *_identity: None,
    )
    request = SimpleNamespace(
        tool_call={
            "name": "remember",
            "id": "call-remember",
            "args": arguments,
        }
    )
    with tool_runtime_scope(runtime):
        retried = execute_tool_call(request, lambda _request: None)

    assert delayed == {"status": "failed", "error": "SIDE_EFFECT_OUTCOME_UNKNOWN"}
    assert runner_calls == 0
    assert isinstance(retried, ToolMessage)
    assert "SIDE_EFFECT_OUTCOME_UNKNOWN" in str(retried.content)
    assert dispatched == []
    with Session(get_engine()) as session:
        assert session.exec(select(SideEffectReceipt)).all() == []
        assert session.exec(select(MemoryRecord)).all() == []
