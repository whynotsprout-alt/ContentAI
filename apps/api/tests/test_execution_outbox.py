from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from contentai.agent.runtime.execution_services import AgentPostExecutionService
from contentai.core.config import Settings, get_settings
from contentai.db.session import get_engine
from contentai.memory.execution_state import ExecutionLeaseLost, ExecutionStateManager
from contentai.models.agent import AgentProfile, AgentVersion
from contentai.models.base import utcnow
from contentai.models.chat import (
    AgentExecution,
    AgentExecutionAttempt,
    AgentInvocation,
    ChatMessage,
    ChatSession,
    ExecutionOutbox,
    ExecutionResumeRequest,
)
from contentai.models.enums import ExecutionAttemptStatus, MessageRole, RunStatus
from contentai.models.user import AppUser
from contentai.services import dispatcher as dispatcher_module
from contentai.services import tasks as tasks_module
from contentai.services.execution_claim import claim_execution
from langchain_core.messages import HumanMessage, message_to_dict
from model_config_helpers import DEFAULT_MODEL_CONFIG_ID
from sqlmodel import Session, select


def _durable_turn_context_payload(
    *,
    execution_id: str,
    invocation_id: str,
    session_id: str,
    user_id: str,
    agent_id: str,
    agent_version_id: str,
    message_id: str,
    tool_permissions: list[str] | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "version": 1,
        "lineage": {
            "execution_id": execution_id,
            "invocation_id": invocation_id,
            "session_id": session_id,
            "user_id": user_id,
            "agent_id": agent_id,
            "agent_version_id": agent_version_id,
            "message_id": message_id,
        },
        "auth": {
            "role": "user",
            "tool_permissions": tool_permissions or ["prepare_topic_research"],
        },
        "turn_context": {
            "focus_message": "durable snapshot input",
            "system_prompt": "durable system prompt",
            "messages": [message_to_dict(HumanMessage(content="durable snapshot input"))],
            "short_term_summary": "durable summary",
            "long_term_memories": [],
        },
    }
    digest = hashlib.sha256(
        json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {**body, "digest": digest}


def _settings(*, max_execution_attempts: int = 3) -> Settings:
    settings = get_settings().model_copy(deep=True)
    settings.agent.max_execution_attempts = max_execution_attempts
    return settings


def _seed_execution(
    settings: Settings,
    *,
    execution_id: str,
    status: RunStatus = RunStatus.pending,
    attempt_count: int = 0,
    lease_expires_at: datetime | None = None,
    worker_id: str | None = None,
) -> None:
    user_id = f"user-{execution_id}"
    session_id = f"session-{execution_id}"
    invocation_id = f"invocation-{execution_id}"
    agent_id = f"agent-{execution_id}"
    version_id = f"version-{execution_id}"
    with Session(get_engine(settings)) as session:
        session.add(
            AppUser(
                id=user_id,
                email=f"{execution_id}@example.test",
                email_normalized=f"{execution_id}@example.test",
                password_hash="test-hash",
                status="active",
                email_verified_at=utcnow(),
            )
        )
        session.flush()
        session.add(AgentProfile(id=agent_id, user_id=user_id, name=f"Agent {execution_id}"))
        session.flush()
        session.add(
            AgentVersion(
                id=version_id,
                agent_id=agent_id,
                version=1,
                content_prompt="Create test content.",
            )
        )
        session.flush()
        session.add(
            ChatSession(
                id=session_id,
                agent_id=agent_id,
                agent_version_id=version_id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
                user_id=user_id,
            )
        )
        session.flush()
        session.add(
            AgentInvocation(
                id=invocation_id,
                session_id=session_id,
                agent_id=agent_id,
                user_id=user_id,
            )
        )
        session.flush()
        session.add(
            AgentExecution(
                id=execution_id,
                invocation_id=invocation_id,
                session_id=session_id,
                agent_version_id=version_id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
                status=status,
                attempt_count=attempt_count,
                lease_expires_at=lease_expires_at,
                worker_id=worker_id,
            )
        )
        session.add(
            ChatMessage(
                id=f"message-{execution_id}",
                session_id=session_id,
                invocation_id=invocation_id,
                role=MessageRole.user,
                content="durable snapshot input",
            )
        )
        session.commit()


def test_claim_restores_tool_permissions_only_from_validated_durable_outbox_payload() -> None:
    settings = _settings()
    execution_id = "execution-durable-turn-payload"
    _seed_execution(settings, execution_id=execution_id)
    with Session(get_engine(settings)) as session:
        session.add(
            ExecutionOutbox(
                execution_id=execution_id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
                kind="execute",
                payload=_durable_turn_context_payload(
                    execution_id=execution_id,
                    invocation_id=f"invocation-{execution_id}",
                    session_id=f"session-{execution_id}",
                    user_id=f"user-{execution_id}",
                    agent_id=f"agent-{execution_id}",
                    agent_version_id=f"version-{execution_id}",
                    message_id=f"message-{execution_id}",
                ),
            )
        )
        session.commit()

    claimed = claim_execution(
        SimpleNamespace(settings=settings),
        execution_id,
        "worker-durable-payload",
        create_attempt=False,
        use_lease=False,
    )

    assert claimed is not None
    assert claimed.auth.tool_permissions == ("prepare_topic_research",)


@pytest.mark.parametrize("payload", [{}, {"version": 1}])
def test_claim_fails_closed_for_missing_or_malformed_durable_outbox_payload(
    payload: dict[str, object],
) -> None:
    settings = _settings()
    execution_id = f"execution-invalid-durable-payload-{len(payload)}"
    _seed_execution(settings, execution_id=execution_id)
    with Session(get_engine(settings)) as session:
        session.add(
            ExecutionOutbox(
                execution_id=execution_id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
                kind="execute",
                payload=payload,
            )
        )
        session.commit()

    assert (
        claim_execution(
            SimpleNamespace(settings=settings),
            execution_id,
            "worker-invalid-payload",
            create_attempt=False,
            use_lease=False,
        )
        is None
    )
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.status == RunStatus.failed
        assert execution.error == "TURN_CONTEXT_SNAPSHOT_INVALID"


def test_postprocess_processing_retries_are_separate_from_broker_attempts() -> None:
    settings = _settings()
    execution_id = "execution-postprocess-retry"
    _seed_execution(settings, execution_id=execution_id, status=RunStatus.completed)
    with Session(get_engine(settings)) as session:
        session.add(
            ExecutionOutbox(
                id="outbox-postprocess-retry",
                execution_id=execution_id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
                kind="postprocess",
                status="processing",
                attempts=7,
                processing_attempts=1,
            )
        )
        session.commit()

    service = AgentPostExecutionService(container=SimpleNamespace(), settings=settings)
    service._mark_processing_failure(execution_id, "summary failed")

    with Session(get_engine(settings)) as session:
        outbox = session.get(ExecutionOutbox, "outbox-postprocess-retry")
        assert outbox is not None
        assert outbox.status == "pending"
        assert outbox.attempts == 7
        assert outbox.processing_attempts == 1
        assert outbox.available_at > utcnow()
        assert outbox.last_error == "summary failed"


def test_postprocess_third_processing_failure_is_terminal() -> None:
    settings = _settings()
    execution_id = "execution-postprocess-failed"
    _seed_execution(settings, execution_id=execution_id, status=RunStatus.completed)
    with Session(get_engine(settings)) as session:
        session.add(
            ExecutionOutbox(
                id="outbox-postprocess-failed",
                execution_id=execution_id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
                kind="postprocess",
                status="processing",
                processing_attempts=3,
            )
        )
        session.commit()

    service = AgentPostExecutionService(container=SimpleNamespace(), settings=settings)
    service._mark_processing_failure(execution_id, "title failed")

    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        outbox = session.get(ExecutionOutbox, "outbox-postprocess-failed")
        assert execution is not None
        assert execution.status == RunStatus.completed
        assert execution.postprocess_completed_at is None
        assert outbox is not None
        assert outbox.status == "failed"
        assert outbox.last_error == "title failed"


def test_dispatcher_retries_after_publish_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    execution_id = "execution-dispatch-retry"
    _seed_execution(settings, execution_id=execution_id)
    with Session(get_engine(settings)) as session:
        session.add(
            ExecutionOutbox(
                id="outbox-dispatch-retry",
                execution_id=execution_id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
                request_id="request-dispatch-retry",
            )
        )
        session.commit()

    published_calls: list[tuple[str, dict[str, object], str]] = []

    def send_task(name: str, *, kwargs: dict[str, object], queue: str) -> None:
        published_calls.append((name, kwargs, queue))
        if len(published_calls) == 1:
            raise ConnectionError("redis unavailable")

    monkeypatch.setattr(dispatcher_module.celery_app, "send_task", send_task)
    dispatcher = dispatcher_module.OutboxDispatcher(settings, dispatcher_id="dispatcher-test")

    assert dispatcher.dispatch_once() == 0
    with Session(get_engine(settings)) as session:
        outbox = session.get(ExecutionOutbox, "outbox-dispatch-retry")
        assert outbox is not None
        assert outbox.status == "pending"
        assert outbox.attempts == 1
        assert outbox.available_at > utcnow()
        assert outbox.locked_by is None
        assert outbox.locked_until is None
        assert outbox.published_at is None
        assert outbox.last_error == "redis unavailable"

        outbox.available_at = utcnow() - timedelta(seconds=1)
        session.add(outbox)
        session.commit()

    assert dispatcher.dispatch_once() == 1
    assert published_calls == [
        (
            "contentai.execute_agent",
            {
                "execution_id": execution_id,
                "request_id": "request-dispatch-retry",
                "model_config_id": DEFAULT_MODEL_CONFIG_ID,
            },
            settings.agent.celery_queue,
        ),
        (
            "contentai.execute_agent",
            {
                "execution_id": execution_id,
                "request_id": "request-dispatch-retry",
                "model_config_id": DEFAULT_MODEL_CONFIG_ID,
            },
            settings.agent.celery_queue,
        ),
    ]
    with Session(get_engine(settings)) as session:
        outbox = session.get(ExecutionOutbox, "outbox-dispatch-retry")
        assert outbox is not None
        assert outbox.status == "published"
        assert outbox.attempts == 1
        assert outbox.published_at is not None
        assert outbox.last_error == ""


def test_duplicate_delivery_only_claims_execution_once() -> None:
    settings = _settings()
    execution_id = "execution-duplicate-delivery"
    _seed_execution(settings, execution_id=execution_id)
    with Session(get_engine(settings)) as session:
        session.add(
            ExecutionOutbox(
                execution_id=execution_id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
                kind="execute",
                payload=_durable_turn_context_payload(
                    execution_id=execution_id,
                    invocation_id=f"invocation-{execution_id}",
                    session_id=f"session-{execution_id}",
                    user_id=f"user-{execution_id}",
                    agent_id=f"agent-{execution_id}",
                    agent_version_id=f"version-{execution_id}",
                    message_id=f"message-{execution_id}",
                ),
            )
        )
        session.commit()
    service = SimpleNamespace(settings=settings)

    first_claim = claim_execution(
        service,
        execution_id,
        "worker-first",
    )
    duplicate_claim = claim_execution(
        service,
        execution_id,
        "worker-duplicate",
    )

    assert first_claim is not None
    assert first_claim.worker_id == "worker-first"
    assert duplicate_claim is None
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.worker_id == "worker-first"
        assert execution.attempt_count == 1
        assert execution.claimed_at is not None
        assert execution.heartbeat_at is not None
        assert execution.lease_expires_at is not None


def test_worker_rejects_delivery_for_a_different_model_configuration() -> None:
    settings = _settings()
    execution_id = "execution-model-config-fence"
    _seed_execution(settings, execution_id=execution_id)
    with Session(get_engine(settings)) as session:
        session.add(
            ExecutionOutbox(
                execution_id=execution_id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
                kind="execute",
                payload=_durable_turn_context_payload(
                    execution_id=execution_id,
                    invocation_id=f"invocation-{execution_id}",
                    session_id=f"session-{execution_id}",
                    user_id=f"user-{execution_id}",
                    agent_id=f"agent-{execution_id}",
                    agent_version_id=f"version-{execution_id}",
                    message_id=f"message-{execution_id}",
                ),
            )
        )
        session.commit()

    claim = claim_execution(
        SimpleNamespace(settings=settings),
        execution_id,
        "worker-wrong-model",
        model_config_id="different-model-config",
    )

    assert claim is None
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.attempt_count == 0
        assert execution.worker_id is None


def test_worker_ownership_fence_rejects_stale_lease_holder() -> None:
    settings = _settings()
    execution_id = "execution-worker-fence"
    _seed_execution(
        settings,
        execution_id=execution_id,
        status=RunStatus.running,
        attempt_count=1,
        lease_expires_at=utcnow() + timedelta(seconds=30),
        worker_id="worker-old",
    )

    with Session(get_engine(settings)) as worker_session:
        execution = worker_session.get(AgentExecution, execution_id)
        assert execution is not None

        with Session(get_engine(settings)) as recovery_session:
            recovered = recovery_session.get(AgentExecution, execution_id)
            assert recovered is not None
            recovered.worker_id = "worker-new"
            recovered.lease_expires_at = utcnow() + timedelta(seconds=30)
            recovery_session.add(recovered)
            recovery_session.commit()

        with pytest.raises(ExecutionLeaseLost):
            ExecutionStateManager().ensure_execution_not_cancelled(
                worker_session,
                execution,
                expected_worker_id="worker-old",
            )


def test_dispatcher_reclaims_expired_publishing_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    execution_id = "execution-expired-publish-lock"
    _seed_execution(settings, execution_id=execution_id)
    with Session(get_engine(settings)) as session:
        session.add(
            ExecutionOutbox(
                id="outbox-expired-publish-lock",
                execution_id=execution_id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
                status="publishing",
                locked_by="dead-dispatcher",
                locked_until=utcnow() - timedelta(seconds=1),
            )
        )
        session.commit()

    calls: list[dict[str, object]] = []

    def send_task(_name: str, *, kwargs: dict[str, object], queue: str) -> None:
        calls.append({"kwargs": kwargs, "queue": queue})

    monkeypatch.setattr(dispatcher_module.celery_app, "send_task", send_task)

    assert dispatcher_module.OutboxDispatcher(settings).dispatch_once() == 1
    assert calls == [
        {
            "kwargs": {
                "execution_id": execution_id,
                "request_id": None,
                "model_config_id": DEFAULT_MODEL_CONFIG_ID,
            },
            "queue": settings.agent.celery_queue,
        }
    ]
    with Session(get_engine(settings)) as session:
        outbox = session.get(ExecutionOutbox, "outbox-expired-publish-lock")
        assert outbox is not None
        assert outbox.status == "published"
        assert outbox.locked_by is None
        assert outbox.locked_until is None


def test_stale_dispatcher_cannot_overwrite_new_outbox_lock() -> None:
    settings = _settings()
    execution_id = "execution-dispatch-fence"
    _seed_execution(settings, execution_id=execution_id)
    with Session(get_engine(settings)) as session:
        session.add(
            ExecutionOutbox(
                id="outbox-dispatch-fence",
                execution_id=execution_id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
                status="publishing",
                locked_by="dispatcher-new",
                locked_until=utcnow() + timedelta(seconds=30),
            )
        )
        session.commit()

    stale = dispatcher_module.OutboxDispatcher(settings, dispatcher_id="dispatcher-old")
    stale._mark_published("outbox-dispatch-fence")
    stale._mark_retry("outbox-dispatch-fence", "stale failure")

    with Session(get_engine(settings)) as session:
        outbox = session.get(ExecutionOutbox, "outbox-dispatch-fence")
        assert outbox is not None
        assert outbox.status == "publishing"
        assert outbox.locked_by == "dispatcher-new"
        assert outbox.attempts == 0
        assert outbox.published_at is None
        assert outbox.last_error == ""


def test_worker_claim_at_retry_limit_fails_without_running() -> None:
    settings = _settings(max_execution_attempts=2)
    execution_id = "execution-claim-retry-limit"
    _seed_execution(
        settings,
        execution_id=execution_id,
        attempt_count=2,
    )

    claim = claim_execution(
        SimpleNamespace(settings=settings),
        execution_id,
        "worker-too-late",
    )

    assert claim is None
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.status == RunStatus.failed
        assert execution.worker_id is None
        assert execution.attempt_count == 2
        assert execution.finished_at is not None
        assert execution.error == "Execution retry limit exceeded."


def test_worker_claim_settles_cancel_fence_without_creating_attempt() -> None:
    settings = _settings()
    execution_id = "execution-claim-cancel-fence"
    _seed_execution(settings, execution_id=execution_id)
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        execution.cancel_requested_at = utcnow()
        session.add(execution)
        session.add(
            ExecutionOutbox(
                id="outbox-claim-cancel-fence",
                execution_id=execution_id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
                status="published",
                published_at=utcnow(),
                payload=_durable_turn_context_payload(
                    execution_id=execution_id,
                    invocation_id=f"invocation-{execution_id}",
                    session_id=f"session-{execution_id}",
                    user_id=f"user-{execution_id}",
                    agent_id=f"agent-{execution_id}",
                    agent_version_id=f"version-{execution_id}",
                    message_id=f"message-{execution_id}",
                ),
            )
        )
        session.commit()

    claimed = claim_execution(
        SimpleNamespace(settings=settings),
        execution_id,
        "worker-cancel-fence",
    )

    assert claimed is None
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        outbox = session.get(ExecutionOutbox, "outbox-claim-cancel-fence")
        attempts = session.exec(
            select(AgentExecutionAttempt).where(
                AgentExecutionAttempt.execution_id == execution_id
            )
        ).all()
        assert execution is not None
        assert execution.status == RunStatus.cancelled
        assert execution.attempt_count == 0
        assert execution.finished_at is not None
        assert outbox is not None
        assert outbox.status == "cancelled"
        assert attempts == []


def test_recover_expired_lease_requeues_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(max_execution_attempts=3)
    execution_id = "execution-expired-lease"
    _seed_execution(
        settings,
        execution_id=execution_id,
        status=RunStatus.running,
        attempt_count=1,
        lease_expires_at=utcnow() - timedelta(seconds=10),
        worker_id="worker-lost",
    )
    with Session(get_engine(settings)) as session:
        session.add(
            ExecutionOutbox(
                id="outbox-expired-lease",
                execution_id=execution_id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
                status="published",
                published_at=utcnow() - timedelta(minutes=1),
            )
        )
        session.commit()

    monkeypatch.setattr(tasks_module, "get_settings", lambda: settings)

    assert tasks_module.recover_expired_executions.run() == 1
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        outbox = session.get(ExecutionOutbox, "outbox-expired-lease")
        assert execution is not None
        assert execution.status == RunStatus.pending
        assert execution.worker_id is None
        assert execution.claimed_at is None
        assert execution.heartbeat_at is None
        assert execution.lease_expires_at is None
        assert execution.attempt_count == 1
        assert outbox is not None
        assert outbox.status == "pending"
        assert outbox.available_at <= utcnow()
        assert outbox.locked_by is None
        assert outbox.locked_until is None


def test_recover_expired_cancelled_lease_settles_all_delivery_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(max_execution_attempts=3)
    execution_id = "execution-expired-cancel-fence"
    _seed_execution(
        settings,
        execution_id=execution_id,
        status=RunStatus.running,
        attempt_count=1,
        lease_expires_at=utcnow() - timedelta(seconds=10),
        worker_id="worker-cancelled",
    )
    with Session(get_engine(settings)) as session:
        attempt = AgentExecutionAttempt(
            id="attempt-expired-cancel-fence",
            execution_id=execution_id,
            ordinal=1,
            worker_id="worker-cancelled",
        )
        session.add(attempt)
        session.flush()
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        execution.current_attempt_id = attempt.id
        execution.cancel_requested_at = utcnow() - timedelta(seconds=5)
        session.add(execution)
        session.add(
            ExecutionOutbox(
                id="outbox-expired-cancel-fence",
                execution_id=execution_id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
                status="published",
                published_at=utcnow() - timedelta(minutes=1),
            )
        )
        session.add(
            ExecutionResumeRequest(
                id="resume-expired-cancel-fence",
                execution_id=execution_id,
                interrupt_id="interrupt-expired-cancel-fence",
                status="claimed",
                claimed_by="worker-cancelled",
                claimed_at=utcnow() - timedelta(minutes=1),
            )
        )
        session.commit()

    monkeypatch.setattr(tasks_module, "get_settings", lambda: settings)

    assert tasks_module.recover_expired_executions.run() == 0
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        attempt = session.get(AgentExecutionAttempt, "attempt-expired-cancel-fence")
        outbox = session.get(ExecutionOutbox, "outbox-expired-cancel-fence")
        resume = session.get(ExecutionResumeRequest, "resume-expired-cancel-fence")
        assert execution is not None
        assert execution.status == RunStatus.cancelled
        assert execution.finished_at is not None
        assert attempt is not None
        assert attempt.status == ExecutionAttemptStatus.cancelled
        assert attempt.finished_at is not None
        assert outbox is not None
        assert outbox.status == "cancelled"
        assert outbox.locked_by is None
        assert outbox.locked_until is None
        assert resume is not None
        assert resume.status == "stale"


def test_expired_lease_at_retry_limit_fails_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(max_execution_attempts=2)
    execution_id = "execution-retry-limit"
    _seed_execution(
        settings,
        execution_id=execution_id,
        status=RunStatus.running,
        attempt_count=2,
        lease_expires_at=utcnow() - timedelta(seconds=10),
        worker_id="worker-lost",
    )
    monkeypatch.setattr(tasks_module, "get_settings", lambda: settings)

    assert tasks_module.recover_expired_executions.run() == 0
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.status == RunStatus.failed
        assert execution.finished_at is not None
        assert execution.error == ("Execution retry limit exceeded after worker lease expiry.")
        assert session.get(ExecutionOutbox, "outbox-retry-limit") is None


def test_recovery_fails_execution_that_no_worker_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    settings.agent.worker_claim_timeout_seconds = 1
    execution_id = "execution-never-claimed"
    _seed_execution(settings, execution_id=execution_id)
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        execution.created_at = utcnow() - timedelta(seconds=2)
        session.add(execution)
        session.add(
            ExecutionOutbox(
                execution_id=execution_id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
                kind="execute",
                status="published",
                published_at=utcnow() - timedelta(seconds=2),
            )
        )
        session.commit()

    monkeypatch.setattr(tasks_module, "get_settings", lambda: settings)

    assert tasks_module.recover_expired_executions.run() == 0
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.status == RunStatus.failed
        assert execution.finished_at is not None
        assert execution.error == "Agent worker did not claim the execution within 1 seconds."


def test_recovery_does_not_fail_execution_before_outbox_is_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    settings.agent.worker_claim_timeout_seconds = 1
    execution_id = "execution-awaiting-publish"
    _seed_execution(settings, execution_id=execution_id)
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        execution.created_at = utcnow() - timedelta(seconds=30)
        session.add(execution)
        session.add(
            ExecutionOutbox(
                execution_id=execution_id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
                kind="execute",
                status="pending",
                available_at=utcnow() - timedelta(seconds=30),
            )
        )
        session.commit()

    monkeypatch.setattr(tasks_module, "get_settings", lambda: settings)

    assert tasks_module.recover_expired_executions.run() == 0
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        outbox = session.exec(
            select(ExecutionOutbox).where(
                ExecutionOutbox.execution_id == execution_id,
                ExecutionOutbox.kind == "execute",
            )
        ).one()
        assert execution is not None
        assert execution.status == RunStatus.pending
        assert execution.finished_at is None
        assert outbox.status == "pending"
