from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from celery.exceptions import Reject
from contentai.agent.runtime.checkpoint import CheckpointOwnershipLostError
from contentai.agent.runtime.execution_services import (
    AgentPostExecutionService,
    AgentRuntimeEventService,
)
from contentai.agent.runtime.runner import AgentRunner
from contentai.agent.runtime.turn_context import DurableTurnContext
from contentai.core.config import Settings, get_settings
from contentai.core.security import AuthContext
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
from contentai.services.execution_claim import (
    CheckpointProbeUnstableError,
    ClaimedExecution,
    claim_execution,
)
from contentai.services.execution_resume import stable_json_hash
from contentai.services.execution_settlement import (
    current_database_time,
    finish_current_attempt,
    settle_execution_failure,
)
from direct_dispatcher import DirectDispatcher
from langchain_core.messages import HumanMessage, message_to_dict
from langgraph.types import Command
from model_config_helpers import DEFAULT_MODEL_CONFIG_ID
from psycopg import OperationalError as PsycopgOperationalError
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import text
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


def _seed_execute_outbox(settings: Settings, execution_id: str) -> None:
    with Session(get_engine(settings)) as session:
        session.add(
            ExecutionOutbox(
                id=f"outbox-{execution_id}",
                execution_id=execution_id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
                kind="execute",
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


def _seed_resume_request(
    settings: Settings,
    execution_id: str,
    *,
    interrupt_id: str,
    tool_calls: list[dict[str, Any]],
    status: str = "pending",
) -> None:
    with Session(get_engine(settings)) as session:
        session.add(
            ExecutionResumeRequest(
                id=f"resume-{execution_id}",
                execution_id=execution_id,
                interrupt_id=interrupt_id,
                decision="approve",
                tool_calls_hash=stable_json_hash(tool_calls),
                status=status,
                claimed_by="previous-worker" if status == "claimed" else None,
                claimed_at=utcnow() if status == "claimed" else None,
            )
        )
        session.commit()


def _seed_recovery_resume_states(settings: Settings, execution_id: str) -> None:
    claimed_at = utcnow() - timedelta(minutes=1)
    with Session(get_engine(settings)) as session:
        session.add_all(
            [
                ExecutionResumeRequest(
                    id=f"resume-{execution_id}-pending",
                    execution_id=execution_id,
                    interrupt_id=f"interrupt-{execution_id}-pending",
                    status="pending",
                ),
                ExecutionResumeRequest(
                    id=f"resume-{execution_id}-claimed",
                    execution_id=execution_id,
                    interrupt_id=f"interrupt-{execution_id}-claimed",
                    status="claimed",
                    claimed_by="stale-recovery-worker",
                    claimed_at=claimed_at,
                ),
                ExecutionResumeRequest(
                    id=f"resume-{execution_id}-consumed",
                    execution_id=execution_id,
                    interrupt_id=f"interrupt-{execution_id}-consumed",
                    status="consumed",
                    claimed_by="finished-recovery-worker",
                    claimed_at=claimed_at,
                    consumed_at=claimed_at,
                ),
            ]
        )
        session.commit()


def _assert_recovery_resume_states(settings: Settings, execution_id: str) -> None:
    with Session(get_engine(settings)) as session:
        requests = {
            request.status: request
            for request in session.exec(
                select(ExecutionResumeRequest)
                .where(ExecutionResumeRequest.execution_id == execution_id)
                .order_by(ExecutionResumeRequest.id)
            ).all()
        }
        stale = [
            request
            for request in session.exec(
                select(ExecutionResumeRequest).where(
                    ExecutionResumeRequest.execution_id == execution_id,
                    ExecutionResumeRequest.status == "stale",
                )
            ).all()
        ]
        assert len(stale) == 2
        assert all(request.claimed_by is None for request in stale)
        assert all(request.claimed_at is None for request in stale)
        consumed = requests["consumed"]
        assert consumed.claimed_by == "finished-recovery-worker"
        assert consumed.claimed_at is not None
        assert consumed.consumed_at is not None


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_runner_resume_context_uses_original_input_not_decision_message(
    decision: str,
) -> None:
    settings = _settings()
    execution_id = f"execution-resume-original-message-{decision}"
    _seed_execution(settings, execution_id=execution_id, status=RunStatus.waiting_input)
    _seed_execute_outbox(settings, execution_id)
    with Session(get_engine(settings)) as session:
        session.add(
            ChatMessage(
                id=f"decision-message-{decision}",
                session_id=f"session-{execution_id}",
                invocation_id=f"invocation-{execution_id}",
                execution_id=execution_id,
                role=MessageRole.user,
                content=f"approval decision: {decision}",
            )
        )
        session.commit()

    runner = object.__new__(AgentRunner)
    with Session(get_engine(settings)) as session:
        loaded = runner._load_execution_context(
            session,
            execution_id,
            AuthContext(
                user_id=f"user-{execution_id}",
                role="user",
                allowed_agent_ids=(f"agent-{execution_id}",),
                tool_permissions=(),
            ),
        )

    assert loaded is not None
    _execution, _invocation, _chat, user_message = loaded
    assert user_message.id == f"message-{execution_id}"
    assert user_message.content == "durable snapshot input"
    assert user_message.execution_id is None


_CHECKPOINT_ID_ABSENT = object()


def _checkpoint_tuple(
    checkpoint_id: Any = _CHECKPOINT_ID_ABSENT,
    *,
    interrupts: list[dict[str, Any]] | None = None,
) -> SimpleNamespace:
    configurable: dict[str, Any] = {}
    if checkpoint_id is not _CHECKPOINT_ID_ABSENT:
        configurable["checkpoint_id"] = checkpoint_id
    return SimpleNamespace(
        config={"configurable": configurable},
        checkpoint={"channel_values": {}},
        pending_writes=(
            [("task", "__interrupt__", list(interrupts))] if interrupts else []
        ),
        tasks=(),
    )


class _SequenceCheckpointer:
    def __init__(
        self,
        checkpoints: list[SimpleNamespace | None],
        *,
        on_get: Any = None,
    ) -> None:
        self.checkpoints = checkpoints
        self.on_get = on_get
        self.calls = 0
        self.configs: list[dict[str, Any]] = []

    def get_tuple(self, config: dict[str, Any]) -> SimpleNamespace | None:
        self.calls += 1
        self.configs.append(config)
        if self.on_get is not None:
            self.on_get(self.calls)
        return self.checkpoints[min(self.calls - 1, len(self.checkpoints) - 1)]


def _checkpoint_service(
    settings: Settings,
    checkpointer: _SequenceCheckpointer,
) -> SimpleNamespace:
    return SimpleNamespace(
        settings=settings,
        runtime=SimpleNamespace(get_checkpointer=lambda: checkpointer),
    )


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


def test_claim_failure_ends_expired_current_attempt_and_live_delivery_state() -> None:
    settings = _settings(max_execution_attempts=2)
    execution_id = "execution-claim-failure-lineage"
    attempt_id = "attempt-claim-failure-lineage"
    _seed_execution(
        settings,
        execution_id=execution_id,
        status=RunStatus.running,
        attempt_count=2,
        lease_expires_at=utcnow() - timedelta(seconds=1),
        worker_id="expired-worker",
    )
    _seed_execute_outbox(settings, execution_id)
    _seed_resume_request(
        settings,
        execution_id,
        interrupt_id="interrupt-claim-failure-lineage",
        tool_calls=[],
        status="claimed",
    )
    with Session(get_engine(settings)) as session:
        session.add(
            AgentExecutionAttempt(
                id=attempt_id,
                execution_id=execution_id,
                ordinal=2,
                worker_id="expired-worker",
                status=ExecutionAttemptStatus.running,
            )
        )
        session.flush()
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        execution.current_attempt_id = attempt_id
        session.add(execution)
        session.commit()

    assert (
        claim_execution(
            SimpleNamespace(settings=settings),
            execution_id,
            "replacement-worker",
        )
        is None
    )

    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        attempt = session.get(AgentExecutionAttempt, attempt_id)
        outbox = session.get(ExecutionOutbox, f"outbox-{execution_id}")
        resume = session.get(ExecutionResumeRequest, f"resume-{execution_id}")
        assert execution is not None
        assert execution.status == RunStatus.failed
        assert execution.error == "Execution retry limit exceeded."
        assert execution.streaming_degraded is True
        assert execution.streaming_degraded_reason == "TERMINAL_STREAM_NOT_PUBLISHED"
        assert attempt is not None
        assert attempt.status == ExecutionAttemptStatus.lease_lost
        assert attempt.finished_at == execution.finished_at
        assert outbox is not None
        assert outbox.status == "failed"
        assert outbox.last_error == execution.error
        assert resume is not None
        assert resume.status == "stale"


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


@pytest.mark.parametrize("resume_status", ["pending", "claimed"])
def test_checkpoint_observation_uses_one_tuple_for_interrupt_and_id(
    resume_status: str,
) -> None:
    settings = _settings()
    execution_id = f"execution-checkpoint-single-snapshot-{resume_status}"
    interrupt_id = f"interrupt-{resume_status}"
    tool_calls = [
        {
            "name": "remember",
            "args": {"content": f"durable {resume_status} preference"},
        }
    ]
    interrupt = {
        "id": interrupt_id,
        "value": {"tool_calls": tool_calls},
    }
    _seed_execution(settings, execution_id=execution_id)
    _seed_execute_outbox(settings, execution_id)
    _seed_resume_request(
        settings,
        execution_id,
        interrupt_id=interrupt_id,
        tool_calls=tool_calls,
        status=resume_status,
    )
    checkpointer = _SequenceCheckpointer(
        [
            _checkpoint_tuple("checkpoint-A", interrupts=[interrupt]),
            _checkpoint_tuple("checkpoint-B"),
        ]
    )
    claimed = claim_execution(
        _checkpoint_service(settings, checkpointer),
        execution_id,
        "single-snapshot-worker",
        use_lease=False,
    )

    assert claimed is not None
    assert checkpointer.calls == 1
    assert claimed.checkpoint_id == "checkpoint-A"
    assert claimed.resume_value == {"decision": "approve"}
    assert not claimed.continue_from_checkpoint


@pytest.mark.parametrize(
    ("case", "checkpoint_id"),
    [
        ("absent", _CHECKPOINT_ID_ABSENT),
        ("none", None),
        ("empty", ""),
        ("spaces", "   "),
        ("padded", " checkpoint-A "),
        ("non-string", 42),
    ],
)
@pytest.mark.parametrize(
    ("mode", "expected_error", "initial_attempt_count"),
    [
        ("resume", "CHECKPOINT_VALIDATION_FAILED", 0),
        ("retry", "CHECKPOINT_RECOVERY_FAILED", 1),
    ],
)
def test_checkpoint_id_is_strict_and_fails_closed(
    case: str,
    checkpoint_id: Any,
    mode: str,
    expected_error: str,
    initial_attempt_count: int,
) -> None:
    settings = _settings()
    execution_id = f"execution-invalid-checkpoint-{mode}-{case}"
    _seed_execution(
        settings,
        execution_id=execution_id,
        attempt_count=initial_attempt_count,
    )
    _seed_execute_outbox(settings, execution_id)
    if mode == "resume":
        _seed_resume_request(
            settings,
            execution_id,
            interrupt_id="invalid-checkpoint-interrupt",
            tool_calls=[],
        )
    checkpointer = _SequenceCheckpointer([_checkpoint_tuple(checkpoint_id)])

    claimed = claim_execution(
        _checkpoint_service(settings, checkpointer),
        execution_id,
        "invalid-checkpoint-worker",
        use_lease=False,
    )

    assert claimed is None
    assert checkpointer.calls == 1
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        attempts = session.exec(
            select(AgentExecutionAttempt).where(
                AgentExecutionAttempt.execution_id == execution_id
            )
        ).all()
        assert execution is not None
        assert execution.status == RunStatus.failed
        assert execution.error == expected_error
        assert execution.attempt_count == initial_attempt_count
        assert execution.current_attempt_id is None
        assert execution.worker_id is None
        assert attempts == []


def test_missing_checkpoint_makes_resume_stale_without_advancing_worker() -> None:
    settings = _settings()
    execution_id = "execution-missing-resume-checkpoint"
    _seed_execution(settings, execution_id=execution_id)
    _seed_execute_outbox(settings, execution_id)
    _seed_resume_request(
        settings,
        execution_id,
        interrupt_id="missing-resume-interrupt",
        tool_calls=[],
    )
    checkpointer = _SequenceCheckpointer([None])

    claimed = claim_execution(
        _checkpoint_service(settings, checkpointer),
        execution_id,
        "missing-resume-worker",
        use_lease=False,
    )

    assert claimed is None
    assert checkpointer.calls == 1
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        request = session.get(ExecutionResumeRequest, f"resume-{execution_id}")
        assert execution is not None
        assert execution.status == RunStatus.failed
        assert execution.error == "RUN_INTERRUPT_STALE"
        assert execution.attempt_count == 0
        assert execution.current_attempt_id is None
        assert request is not None
        assert request.status == "stale"


@pytest.mark.parametrize(
    ("checkpoint", "expected_continue", "expected_checkpoint_id"),
    [
        (None, False, None),
        (_checkpoint_tuple("checkpoint-END"), True, "checkpoint-END"),
    ],
)
def test_retry_preserves_before_first_checkpoint_and_end_recovery(
    checkpoint: SimpleNamespace | None,
    expected_continue: bool,
    expected_checkpoint_id: str | None,
) -> None:
    settings = _settings()
    suffix = "end" if checkpoint is not None else "before-first"
    execution_id = f"execution-retry-{suffix}-checkpoint"
    _seed_execution(settings, execution_id=execution_id, attempt_count=1)
    _seed_execute_outbox(settings, execution_id)
    checkpointer = _SequenceCheckpointer([checkpoint])

    claimed = claim_execution(
        _checkpoint_service(settings, checkpointer),
        execution_id,
        "retry-checkpoint-worker",
        use_lease=False,
    )

    assert claimed is not None
    assert claimed.continue_from_checkpoint is expected_continue
    assert claimed.checkpoint_id == expected_checkpoint_id
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.attempt_count == 2
        assert execution.current_attempt_id is not None


@pytest.mark.parametrize(
    ("resume_status", "expected_claim"),
    [("pending", False), ("claimed", True)],
)
def test_resume_target_must_still_exist_in_observed_checkpoint(
    resume_status: str,
    expected_claim: bool,
) -> None:
    settings = _settings()
    execution_id = f"execution-resume-target-gone-{resume_status}"
    _seed_execution(settings, execution_id=execution_id)
    _seed_execute_outbox(settings, execution_id)
    _seed_resume_request(
        settings,
        execution_id,
        interrupt_id="gone-interrupt",
        tool_calls=[],
        status=resume_status,
    )
    checkpointer = _SequenceCheckpointer([_checkpoint_tuple("checkpoint-A")])

    claimed = claim_execution(
        _checkpoint_service(settings, checkpointer),
        execution_id,
        "target-gone-worker",
        use_lease=False,
    )

    assert (claimed is not None) is expected_claim
    if claimed is not None:
        assert claimed.checkpoint_id == "checkpoint-A"
        assert claimed.continue_from_checkpoint
        assert claimed.resume_value is None
    else:
        with Session(get_engine(settings)) as session:
            execution = session.get(AgentExecution, execution_id)
            request = session.get(ExecutionResumeRequest, f"resume-{execution_id}")
            assert execution is not None
            assert execution.error == "RUN_INTERRUPT_STALE"
            assert execution.attempt_count == 0
            assert request is not None
            assert request.status == "stale"


def test_checkpoint_probe_cancellation_fence_wins_without_attempt_or_graph() -> None:
    settings = _settings()
    execution_id = "execution-cancel-during-checkpoint-probe"
    interrupt_id = "interrupt-cancel-during-probe"
    tool_calls = [{"name": "remember", "args": {"content": "must not run"}}]
    interrupt = {"id": interrupt_id, "value": {"tool_calls": tool_calls}}
    _seed_execution(settings, execution_id=execution_id)
    _seed_execute_outbox(settings, execution_id)
    _seed_resume_request(
        settings,
        execution_id,
        interrupt_id=interrupt_id,
        tool_calls=tool_calls,
    )

    def cancel_execution(_call: int) -> None:
        with Session(get_engine(settings)) as session:
            session.exec(text("SET LOCAL lock_timeout = '250ms'"))
            execution = session.get(AgentExecution, execution_id)
            assert execution is not None
            execution.cancel_requested_at = utcnow()
            session.add(execution)
            session.commit()

    checkpointer = _SequenceCheckpointer(
        [_checkpoint_tuple("checkpoint-A", interrupts=[interrupt])],
        on_get=cancel_execution,
    )

    claimed = claim_execution(
        _checkpoint_service(settings, checkpointer),
        execution_id,
        "cancel-probe-worker",
        use_lease=False,
    )

    assert claimed is None
    assert checkpointer.calls == 1
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        attempts = session.exec(
            select(AgentExecutionAttempt).where(
                AgentExecutionAttempt.execution_id == execution_id
            )
        ).all()
        assert execution is not None
        assert execution.status == RunStatus.cancelled
        assert execution.attempt_count == 0
        assert execution.current_attempt_id is None
        assert attempts == []


def test_unstable_checkpoint_fence_raises_without_advancing_attempt() -> None:
    settings = _settings()
    execution_id = "execution-unstable-checkpoint-fence"
    _seed_execution(settings, execution_id=execution_id, attempt_count=1)
    _seed_execute_outbox(settings, execution_id)

    def move_business_fence(call: int) -> None:
        with Session(get_engine(settings)) as session:
            session.exec(text("SET LOCAL lock_timeout = '250ms'"))
            execution = session.get(AgentExecution, execution_id)
            assert execution is not None
            execution.latest_checkpoint_id = f"newer-business-fence-{call}"
            execution.touch_updated_at()
            session.add(execution)
            session.commit()

    checkpointer = _SequenceCheckpointer(
        [_checkpoint_tuple("checkpoint-A")],
        on_get=move_business_fence,
    )

    with pytest.raises(CheckpointProbeUnstableError, match="did not stabilize"):
        claim_execution(
            _checkpoint_service(settings, checkpointer),
            execution_id,
            "unstable-probe-worker",
            use_lease=False,
        )

    assert checkpointer.calls == 3
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        attempts = session.exec(
            select(AgentExecutionAttempt).where(
                AgentExecutionAttempt.execution_id == execution_id
            )
        ).all()
        assert execution is not None
        assert execution.status == RunStatus.pending
        assert execution.attempt_count == 1
        assert execution.current_attempt_id is None
        assert execution.worker_id is None
        assert attempts == []


def test_transient_checkpoint_operational_error_retries_then_claims_normally() -> None:
    settings = _settings()
    execution_id = "execution-transient-checkpoint-operational-error"
    _seed_execution(settings, execution_id=execution_id, attempt_count=1)
    _seed_execute_outbox(settings, execution_id)

    def fail_first_probe(call: int) -> None:
        if call == 1:
            raise PsycopgOperationalError("temporary checkpoint connection failure")

    checkpointer = _SequenceCheckpointer([None], on_get=fail_first_probe)
    service = _checkpoint_service(settings, checkpointer)

    with pytest.raises(CheckpointProbeUnstableError, match="temporarily unavailable"):
        claim_execution(service, execution_id, "transient-probe-worker")

    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.status == RunStatus.pending
        assert execution.attempt_count == 1
        assert execution.current_attempt_id is None
        assert execution.worker_id is None

    claimed = claim_execution(service, execution_id, "transient-probe-worker")

    assert claimed is not None
    assert claimed.attempt_id is not None
    assert checkpointer.calls == 2
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        attempt = session.get(AgentExecutionAttempt, claimed.attempt_id)
        assert execution is not None
        assert execution.attempt_count == 2
        assert execution.current_attempt_id == claimed.attempt_id
        assert execution.lease_expires_at is not None
        assert attempt is not None
        assert attempt.status == ExecutionAttemptStatus.running


def test_execute_agent_retries_only_unstable_checkpoint_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    execution_id = "execution-task-unstable-checkpoint"
    _seed_execution(settings, execution_id=execution_id)
    _seed_execute_outbox(settings, execution_id)
    runner_calls: list[dict[str, Any]] = []
    service = SimpleNamespace(
        settings=settings,
        runner=SimpleNamespace(run=lambda *_args, **kwargs: runner_calls.append(kwargs)),
    )
    retry_call: dict[str, Any] = {}

    class RetryScheduled(Exception):
        pass

    def raise_unstable(*_args: Any, **_kwargs: Any) -> None:
        raise CheckpointProbeUnstableError("unstable checkpoint probe")

    def schedule_retry(**kwargs: Any) -> None:
        retry_call.update(kwargs)
        raise RetryScheduled

    def forbidden_heartbeat(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("heartbeat must not start before a successful claim")

    monkeypatch.setattr(tasks_module, "_agent_service", lambda: service)
    monkeypatch.setattr(tasks_module, "claim_execution", raise_unstable)
    monkeypatch.setattr(tasks_module, "WorkerHeartbeat", forbidden_heartbeat)
    monkeypatch.setattr(tasks_module.execute_agent, "retry", schedule_retry)

    with pytest.raises(RetryScheduled):
        tasks_module.execute_agent.run(
            execution_id=execution_id,
            model_config_id=DEFAULT_MODEL_CONFIG_ID,
            request_id="request-unstable-checkpoint",
        )

    assert isinstance(retry_call["exc"], CheckpointProbeUnstableError)
    assert retry_call["kwargs"] == {
        "execution_id": execution_id,
        "model_config_id": DEFAULT_MODEL_CONFIG_ID,
        "request_id": "request-unstable-checkpoint",
    }
    assert 0 < retry_call["countdown"] <= 30
    assert retry_call["max_retries"] == tasks_module.CHECKPOINT_PROBE_TASK_MAX_RETRIES
    assert runner_calls == []
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.attempt_count == 0
        assert execution.current_attempt_id is None
        assert execution.worker_id is None


def test_execute_agent_requeues_original_delivery_when_retry_publish_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    execution_id = "execution-task-checkpoint-retry-publish-rejected"
    _seed_execution(settings, execution_id=execution_id)
    _seed_execute_outbox(settings, execution_id)
    runner_calls: list[dict[str, Any]] = []
    service = SimpleNamespace(
        settings=settings,
        runner=SimpleNamespace(run=lambda *_args, **kwargs: runner_calls.append(kwargs)),
    )

    def raise_unstable(*_args: Any, **_kwargs: Any) -> None:
        raise CheckpointProbeUnstableError("unstable checkpoint probe")

    def reject_retry_publish(**_kwargs: Any) -> None:
        raise Reject("checkpoint retry publish failed", requeue=False)

    def forbidden_heartbeat(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("heartbeat must not start before a successful claim")

    monkeypatch.setattr(tasks_module, "_agent_service", lambda: service)
    monkeypatch.setattr(tasks_module, "claim_execution", raise_unstable)
    monkeypatch.setattr(tasks_module, "WorkerHeartbeat", forbidden_heartbeat)
    monkeypatch.setattr(tasks_module.execute_agent, "retry", reject_retry_publish)

    with pytest.raises(Reject) as rejected:
        tasks_module.execute_agent.run(
            execution_id=execution_id,
            model_config_id=DEFAULT_MODEL_CONFIG_ID,
            request_id="request-checkpoint-retry-publish-rejected",
        )

    assert rejected.value.reason == "checkpoint retry publish failed"
    assert rejected.value.requeue is True
    assert runner_calls == []
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.attempt_count == 0
        assert execution.current_attempt_id is None
        assert execution.worker_id is None


def test_execute_agent_returns_exhausted_probe_to_durable_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    execution_id = "execution-task-checkpoint-retry-exhausted"
    _seed_execution(settings, execution_id=execution_id)
    _seed_execute_outbox(settings, execution_id)
    runner_calls: list[dict[str, Any]] = []
    service = SimpleNamespace(
        settings=settings,
        runner=SimpleNamespace(run=lambda *_args, **kwargs: runner_calls.append(kwargs)),
    )

    def raise_unstable(*_args: Any, **_kwargs: Any) -> None:
        raise CheckpointProbeUnstableError("checkpoint retry limit exhausted")

    def exhaust_retry(*, exc: Exception, **_kwargs: Any) -> None:
        raise exc

    monkeypatch.setattr(tasks_module, "_agent_service", lambda: service)
    monkeypatch.setattr(tasks_module, "claim_execution", raise_unstable)
    monkeypatch.setattr(
        tasks_module,
        "WorkerHeartbeat",
        lambda *_args, **_kwargs: pytest.fail("heartbeat must not start"),
    )
    monkeypatch.setattr(tasks_module.execute_agent, "retry", exhaust_retry)

    tasks_module.execute_agent.run(
        execution_id=execution_id,
        model_config_id=DEFAULT_MODEL_CONFIG_ID,
        request_id="request-checkpoint-retry-exhausted",
    )

    assert runner_calls == []
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.attempt_count == 0
        assert execution.current_attempt_id is None
        assert execution.worker_id is None
        outbox = session.get(ExecutionOutbox, f"outbox-{execution_id}")
        assert outbox is not None
        assert outbox.status == "pending"
        assert outbox.processing_attempts == 1
        assert outbox.last_error == "CheckpointProbeUnstableError"
        assert outbox.available_at > outbox.updated_at


@pytest.mark.parametrize("case", ["terminal", "lease", "cancel"])
def test_execute_agent_does_not_retry_normal_unclaimable_state(
    case: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    execution_id = f"execution-task-no-retry-{case}"
    if case == "terminal":
        _seed_execution(settings, execution_id=execution_id, status=RunStatus.completed)
    elif case == "lease":
        _seed_execution(
            settings,
            execution_id=execution_id,
            lease_expires_at=utcnow() + timedelta(seconds=30),
            worker_id="active-worker",
        )
    else:
        _seed_execution(settings, execution_id=execution_id)
        with Session(get_engine(settings)) as session:
            execution = session.get(AgentExecution, execution_id)
            assert execution is not None
            execution.cancel_requested_at = utcnow()
            session.add(execution)
            session.commit()

    service = SimpleNamespace(
        settings=settings,
        runner=SimpleNamespace(
            run=lambda *_args, **_kwargs: pytest.fail("runner must not start")
        ),
    )
    monkeypatch.setattr(tasks_module, "_agent_service", lambda: service)
    monkeypatch.setattr(
        tasks_module,
        "WorkerHeartbeat",
        lambda *_args, **_kwargs: pytest.fail("heartbeat must not start"),
    )
    monkeypatch.setattr(
        tasks_module.execute_agent,
        "retry",
        lambda **_kwargs: pytest.fail("ordinary None claims must not retry"),
    )

    assert (
        tasks_module.execute_agent.run(
            execution_id=execution_id,
            model_config_id=DEFAULT_MODEL_CONFIG_ID,
            request_id=f"request-no-retry-{case}",
        )
        is None
    )

    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.attempt_count == 0
        assert execution.current_attempt_id is None
        if case == "cancel":
            assert execution.status == RunStatus.cancelled
        elif case == "terminal":
            assert execution.status == RunStatus.completed
        else:
            assert execution.status == RunStatus.pending
            assert execution.worker_id == "active-worker"


def test_execute_agent_passes_claimed_checkpoint_id_to_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    runner_calls: list[dict[str, Any]] = []
    heartbeat_events: list[str] = []
    service = SimpleNamespace(
        settings=settings,
        runner=SimpleNamespace(run=lambda *_args, **kwargs: runner_calls.append(kwargs)),
    )
    auth = AuthContext(
        user_id="task-checkpoint-user",
        allowed_agent_ids=("task-checkpoint-agent",),
        tool_permissions=("prepare_topic_research",),
    )
    claimed = ClaimedExecution(
        auth=auth,
        turn_context=DurableTurnContext(
            system_prompt="task checkpoint prompt",
            messages=[HumanMessage(content="resume")],
            focus_message="resume",
            role="user",
            tool_permissions=auth.tool_permissions,
        ),
        worker_id="task-checkpoint-worker",
        attempt_id="attempt-task-checkpoint-worker",
        resume_value={"decision": "approve"},
        checkpoint_id="checkpoint-A",
    )

    class RecordingHeartbeat:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            heartbeat_events.append("start")

        def close(self) -> None:
            heartbeat_events.append("close")

    monkeypatch.setattr(tasks_module, "_agent_service", lambda: service)
    monkeypatch.setattr(tasks_module, "claim_execution", lambda *_args, **_kwargs: claimed)
    monkeypatch.setattr(tasks_module, "WorkerHeartbeat", RecordingHeartbeat)

    tasks_module.execute_agent.run(
        execution_id="execution-task-checkpoint-propagation",
        model_config_id=DEFAULT_MODEL_CONFIG_ID,
        request_id="request-task-checkpoint",
    )

    assert heartbeat_events == ["start", "close"]
    assert len(runner_calls) == 1
    assert runner_calls[0]["checkpoint_id"] == "checkpoint-A"
    assert runner_calls[0]["attempt_id"] == "attempt-task-checkpoint-worker"


class _SettlementRecordingWriter:
    def __init__(self, execution_id: str) -> None:
        self.execution_id = execution_id
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.prepared = False

    def emit(self, event_name: str, payload: dict[str, Any]) -> None:
        self.events.append((event_name, dict(payload)))

    def prepare_settlement(self) -> None:
        self.prepared = True

    def drain_settlement(
        self,
        events: list[tuple[str, dict[str, Any]]],
        *,
        db_session: Session | None = None,
    ) -> None:
        assert self.prepared is True
        assert db_session is not None
        execution = db_session.get(AgentExecution, self.execution_id)
        assert execution is not None
        self.events.extend((name, dict(payload)) for name, payload in events)
        execution.stream_committed_sequence += len(events)
        execution.terminal_stream_sequence = execution.stream_committed_sequence
        execution.terminal_stream_attempt_id = execution.current_attempt_id
        execution.terminal_stream_status = execution.status.value
        db_session.add(execution)


def _checkpoint_error_runner(
    settings: Settings,
    *,
    engine: Any,
) -> AgentRunner:
    runner = AgentRunner.__new__(AgentRunner)
    runner.container = SimpleNamespace(settings=settings)
    runner.execution_engine = engine
    runner.event_service = AgentRuntimeEventService(settings)
    runner.post_service = SimpleNamespace()
    return runner


@pytest.mark.parametrize("operation", ["put", "put_writes"])
def test_checkpoint_ownership_loss_observing_cancel_settles_terminal_attempt(
    operation: str,
) -> None:
    settings = _settings()
    execution_id = f"execution-checkpoint-cancel-{operation}"
    worker_id = f"worker-checkpoint-cancel-{operation}"
    attempt_id = f"attempt-checkpoint-cancel-{operation}"
    _seed_execution(
        settings,
        execution_id=execution_id,
        attempt_count=1,
        lease_expires_at=utcnow() + timedelta(minutes=1),
        worker_id=worker_id,
    )
    _seed_execute_outbox(settings, execution_id)
    with Session(get_engine(settings)) as session:
        session.add(
            AgentExecutionAttempt(
                id=attempt_id,
                execution_id=execution_id,
                ordinal=1,
                worker_id=worker_id,
            )
        )
        session.flush()
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        execution.current_attempt_id = attempt_id
        session.add(execution)
        session.commit()

    class CancelThenCheckpointFails:
        @staticmethod
        def run_turn(**_kwargs: Any) -> None:
            with Session(get_engine(settings)) as session:
                session.execute(
                    text(
                        "UPDATE agentexecution "
                        "SET cancel_requested_at = clock_timestamp() "
                        "WHERE id = :execution_id"
                    ),
                    {"execution_id": execution_id},
                )
                session.commit()
            raise CheckpointOwnershipLostError(
                f"checkpoint {operation} observed cancellation"
            )

    writer = _SettlementRecordingWriter(execution_id)
    runner = _checkpoint_error_runner(
        settings,
        engine=CancelThenCheckpointFails(),
    )
    with Session(get_engine(settings)) as session:
        runner.run(
            session,
            execution_id=execution_id,
            worker_id=worker_id,
            attempt_id=attempt_id,
            event_writer=writer,
            turn_context=DurableTurnContext(
                system_prompt="checkpoint cancellation",
                messages=[HumanMessage(content="cancel")],
                focus_message="cancel",
                role="user",
                tool_permissions=("prepare_topic_research",),
            ),
            auth=AuthContext(
                user_id=f"user-{execution_id}",
                allowed_agent_ids=(f"agent-{execution_id}",),
                tool_permissions=("prepare_topic_research",),
            ),
        )

    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        attempt = session.get(AgentExecutionAttempt, attempt_id)
        assert execution is not None
        assert execution.status == RunStatus.cancelled
        assert execution.finished_at is not None
        assert execution.terminal_stream_attempt_id == attempt_id
        assert execution.terminal_stream_status == "cancelled"
        assert attempt is not None
        assert attempt.status == ExecutionAttemptStatus.cancelled
        assert attempt.finished_at is not None
    assert [name for name, _payload in writer.events[-2:]] == [
        "attempt_end",
        "run_cancel",
    ]


def test_checkpoint_ownership_loss_after_takeover_performs_no_old_attempt_settlement() -> None:
    settings = _settings()
    execution_id = "execution-checkpoint-ownership-takeover"
    worker_id = "worker-checkpoint-ownership-old"
    attempt_id = "attempt-checkpoint-ownership-old"
    new_worker_id = "worker-checkpoint-ownership-new"
    new_attempt_id = "attempt-checkpoint-ownership-new"
    _seed_execution(
        settings,
        execution_id=execution_id,
        attempt_count=1,
        lease_expires_at=utcnow() + timedelta(minutes=1),
        worker_id=worker_id,
    )
    _seed_execute_outbox(settings, execution_id)
    with Session(get_engine(settings)) as session:
        session.add(
            AgentExecutionAttempt(
                id=attempt_id,
                execution_id=execution_id,
                ordinal=1,
                worker_id=worker_id,
            )
        )
        session.flush()
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        execution.current_attempt_id = attempt_id
        session.add(execution)
        session.commit()

    class TakeoverThenCheckpointFails:
        @staticmethod
        def run_turn(**_kwargs: Any) -> None:
            with Session(get_engine(settings)) as session:
                now = current_database_time(session)
                old_attempt = session.get(AgentExecutionAttempt, attempt_id)
                execution = session.get(AgentExecution, execution_id)
                assert old_attempt is not None and execution is not None
                old_attempt.status = ExecutionAttemptStatus.lease_lost
                old_attempt.finished_at = now
                session.add(old_attempt)
                session.add(
                    AgentExecutionAttempt(
                        id=new_attempt_id,
                        execution_id=execution_id,
                        ordinal=2,
                        worker_id=new_worker_id,
                    )
                )
                session.flush()
                execution.worker_id = new_worker_id
                execution.current_attempt_id = new_attempt_id
                execution.attempt_count = 2
                execution.lease_expires_at = now + timedelta(minutes=1)
                session.add(execution)
                session.commit()
            raise CheckpointOwnershipLostError("checkpoint owner changed")

    writer = _SettlementRecordingWriter(execution_id)
    runner = _checkpoint_error_runner(settings, engine=TakeoverThenCheckpointFails())
    with Session(get_engine(settings)) as session:
        runner.run(
            session,
            execution_id=execution_id,
            worker_id=worker_id,
            attempt_id=attempt_id,
            event_writer=writer,
            turn_context=DurableTurnContext(
                system_prompt="checkpoint takeover",
                messages=[HumanMessage(content="continue")],
                focus_message="continue",
                role="user",
                tool_permissions=("prepare_topic_research",),
            ),
            auth=AuthContext(
                user_id=f"user-{execution_id}",
                allowed_agent_ids=(f"agent-{execution_id}",),
                tool_permissions=("prepare_topic_research",),
            ),
        )

    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        new_attempt = session.get(AgentExecutionAttempt, new_attempt_id)
        assert execution is not None
        assert execution.status == RunStatus.running
        assert execution.worker_id == new_worker_id
        assert execution.current_attempt_id == new_attempt_id
        assert execution.terminal_stream_sequence is None
        assert new_attempt is not None
        assert new_attempt.status == ExecutionAttemptStatus.running
        assert new_attempt.finished_at is None
    assert all(name not in {"attempt_end", "run_cancel"} for name, _ in writer.events)


@pytest.mark.parametrize("operation", ["put", "put_writes"])
def test_checkpoint_commit_during_probe_forces_claim_to_reprobe(operation: str) -> None:
    from contentai.agent.runtime.checkpoint import (
        RuntimePersistence,
        execution_checkpoint_config,
    )
    from langgraph.checkpoint.base import empty_checkpoint

    settings = _settings()
    execution_id = f"execution-checkpoint-probe-revision-{operation}"
    worker_id = f"worker-checkpoint-probe-revision-{operation}"
    attempt_id = f"attempt-checkpoint-probe-revision-{operation}"
    _seed_execution(
        settings,
        execution_id=execution_id,
        status=RunStatus.running,
        attempt_count=1,
        worker_id=worker_id,
    )
    _seed_execute_outbox(settings, execution_id)
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        chat = session.get(ChatSession, execution.session_id)
        assert chat is not None
        checkpoint_thread_id = chat.langgraph_thread_id
        session.add(
            AgentExecutionAttempt(
                id=attempt_id,
                execution_id=execution_id,
                ordinal=1,
                worker_id=worker_id,
            )
        )
        session.flush()
        execution.current_attempt_id = attempt_id
        session.add(execution)
        session.execute(
            text(
                "UPDATE agentexecution "
                "SET lease_expires_at = clock_timestamp() + interval '1 minute' "
                "WHERE id = :execution_id"
            ),
            {"execution_id": execution_id},
        )
        session.commit()

    persistence = RuntimePersistence(settings)
    checkpointer = persistence.get_checkpointer()
    config = execution_checkpoint_config(
        thread_id=checkpoint_thread_id,
        execution_id=execution_id,
    )
    config["configurable"]["__worker_id"] = worker_id
    config["configurable"]["__attempt_id"] = attempt_id
    first_checkpoint = empty_checkpoint()
    current_config = checkpointer.put(
        config,
        first_checkpoint,
        {"source": "input", "step": 0},
        {},
    )
    with Session(get_engine(settings)) as session:
        session.execute(
            text(
                "UPDATE agentexecution "
                "SET lease_expires_at = clock_timestamp() - interval '1 second' "
                "WHERE id = :execution_id"
            ),
            {"execution_id": execution_id},
        )
        session.commit()
    next_checkpoint = empty_checkpoint()

    class UpdatingCheckpointer:
        def __init__(self) -> None:
            self.calls = 0

        def get_tuple(self, requested_config: dict[str, Any]):
            self.calls += 1
            observed = checkpointer.get_tuple(requested_config)
            if self.calls == 1:
                with Session(get_engine(settings)) as session:
                    session.execute(
                        text(
                            "UPDATE agentexecution "
                            "SET lease_expires_at = "
                            "clock_timestamp() + interval '1 minute' "
                            "WHERE id = :execution_id"
                        ),
                        {"execution_id": execution_id},
                    )
                    session.commit()
                try:
                    if operation == "put":
                        checkpointer.put(
                            current_config,
                            next_checkpoint,
                            {"source": "loop", "step": 1},
                            {},
                        )
                    else:
                        checkpointer.put_writes(
                            current_config,
                            [("custom", {"probe": "advanced"})],
                            "task-probe-revision",
                        )
                finally:
                    with Session(get_engine(settings)) as session:
                        session.execute(
                            text(
                                "UPDATE agentexecution "
                                "SET lease_expires_at = "
                                "clock_timestamp() - interval '1 second' "
                                "WHERE id = :execution_id"
                            ),
                            {"execution_id": execution_id},
                        )
                        session.commit()
            return observed

    observing = UpdatingCheckpointer()
    service = SimpleNamespace(
        settings=settings,
        runtime=SimpleNamespace(get_checkpointer=lambda: observing),
    )
    try:
        claimed = claim_execution(
            service,
            execution_id,
            f"worker-checkpoint-probe-new-{operation}",
            use_lease=False,
        )
    finally:
        persistence.close()

    assert claimed is not None
    assert observing.calls == 2
    assert claimed.checkpoint_id == (
        next_checkpoint["id"] if operation == "put" else first_checkpoint["id"]
    )
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.checkpoint_revision == 2
        assert execution.current_attempt_id == claimed.attempt_id


def test_fenced_event_flush_blocks_takeover_and_never_touches_new_attempt() -> None:
    from contentai.agent.runtime.events import PersistentAgentEventWriter

    settings = _settings()
    execution_id = "execution-event-attempt-fence"
    worker_id = "worker-event-attempt-fence"
    old_attempt_id = "attempt-event-attempt-fence-old"
    new_attempt_id = "attempt-event-attempt-fence-new"
    _seed_execution(
        settings,
        execution_id=execution_id,
        status=RunStatus.running,
        attempt_count=1,
        worker_id=worker_id,
    )
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        session.add_all(
            [
                AgentExecutionAttempt(
                    id=old_attempt_id,
                    execution_id=execution_id,
                    ordinal=1,
                    worker_id=worker_id,
                ),
                AgentExecutionAttempt(
                    id=new_attempt_id,
                    execution_id=execution_id,
                    ordinal=2,
                    worker_id=worker_id,
                ),
            ]
        )
        session.flush()
        execution.current_attempt_id = old_attempt_id
        session.add(execution)
        session.execute(
            text(
                "UPDATE agentexecution "
                "SET lease_expires_at = clock_timestamp() + interval '1 minute' "
                "WHERE id = :execution_id"
            ),
            {"execution_id": execution_id},
        )
        session.commit()

    publish_started = threading.Event()
    release_publish = threading.Event()
    takeover_started = threading.Event()

    class BlockingPublisher:
        def __init__(self) -> None:
            self.events: list[Any] = []

        def publish(self, events: list[Any]) -> None:
            publish_started.set()
            assert release_publish.wait(timeout=3)
            self.events.extend(events)

    publisher = BlockingPublisher()
    writer = PersistentAgentEventWriter(
        execution_id,
        get_engine(settings),
        settings=settings,
        stream_publisher=publisher,
        expected_worker_id=worker_id,
        expected_attempt_id=old_attempt_id,
    )

    def takeover() -> None:
        with Session(get_engine(settings)) as session:
            session.exec(text("SET LOCAL lock_timeout = '3s'"))
            takeover_started.set()
            execution = session.exec(
                select(AgentExecution)
                .where(AgentExecution.id == execution_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one()
            execution.current_attempt_id = new_attempt_id
            execution.attempt_count = 2
            session.add(execution)
            session.commit()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            publish_future = executor.submit(
                writer.emit,
                "assistant_message_delta",
                {"message_id": "message-event-fence", "chunk": "hello", "done": True},
            )
            assert publish_started.wait(timeout=3)
            takeover_future = executor.submit(takeover)
            assert takeover_started.wait(timeout=3)
            with pytest.raises(FutureTimeoutError):
                takeover_future.result(timeout=0.2)
            release_publish.set()
            publish_future.result(timeout=3)
            takeover_future.result(timeout=1)
    finally:
        release_publish.set()

    assert len(publisher.events) == 1
    assert publisher.events[0].payload["attempt_id"] == old_attempt_id
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        old_attempt = session.get(AgentExecutionAttempt, old_attempt_id)
        new_attempt = session.get(AgentExecutionAttempt, new_attempt_id)
        assert execution is not None and execution.first_event_at is not None
        assert old_attempt is not None and old_attempt.first_event_at is not None
        assert new_attempt is not None and new_attempt.first_event_at is None

    writer.emit("state", {"status": "stale-writer-must-drop"})
    writer.close()
    assert len(publisher.events) == 1


def test_same_worker_new_attempt_fences_heartbeat_and_settlement() -> None:
    settings = _settings()
    execution_id = "execution-same-worker-new-attempt"
    worker_id = "worker-reused-delivery-id"
    old_attempt_id = "attempt-reused-worker-old"
    new_attempt_id = "attempt-reused-worker-new"
    _seed_execution(
        settings,
        execution_id=execution_id,
        status=RunStatus.running,
        attempt_count=2,
        worker_id=worker_id,
    )
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        session.add_all(
            [
                AgentExecutionAttempt(
                    id=old_attempt_id,
                    execution_id=execution_id,
                    ordinal=1,
                    worker_id=worker_id,
                ),
                AgentExecutionAttempt(
                    id=new_attempt_id,
                    execution_id=execution_id,
                    ordinal=2,
                    worker_id=worker_id,
                ),
            ]
        )
        session.flush()
        execution.current_attempt_id = new_attempt_id
        execution.heartbeat_at = datetime(2026, 8, 4, tzinfo=utcnow().tzinfo)
        session.add(execution)
        session.commit()
        original_heartbeat = execution.heartbeat_at

    heartbeat = tasks_module.WorkerHeartbeat(
        execution_id,
        worker_id,
        old_attempt_id,
        SimpleNamespace(settings=settings),
    )
    heartbeat.stop_event = SimpleNamespace(wait=lambda _interval: False)
    heartbeat._run()

    with Session(get_engine(settings)) as session:
        execution = session.exec(
            select(AgentExecution)
            .where(AgentExecution.id == execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        assert execution.heartbeat_at == original_heartbeat
        assert not settle_execution_failure(
            session,
            execution,
            now=utcnow(),
            error="stale delivery must not settle",
            expected_worker_id=worker_id,
            expected_attempt_id=old_attempt_id,
        )
        session.commit()

    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        new_attempt = session.get(AgentExecutionAttempt, new_attempt_id)
        assert execution is not None and execution.status == RunStatus.running
        assert execution.error == ""
        assert new_attempt is not None and new_attempt.finished_at is None


@pytest.mark.parametrize(
    ("resume_status", "expected_status"),
    [("pending", "stale"), ("claimed", "stale"), ("consumed", "consumed")],
)
def test_failure_settlement_monotonically_ends_live_resume_request(
    resume_status: str,
    expected_status: str,
) -> None:
    settings = _settings()
    execution_id = f"execution-failure-resume-{resume_status}"
    worker_id = "failure-resume-worker"
    attempt_id = f"attempt-failure-resume-{resume_status}"
    _seed_execution(
        settings,
        execution_id=execution_id,
        status=RunStatus.running,
        attempt_count=1,
        lease_expires_at=utcnow() + timedelta(minutes=1),
        worker_id=worker_id,
    )
    _seed_resume_request(
        settings,
        execution_id,
        interrupt_id=f"interrupt-failure-resume-{resume_status}",
        tool_calls=[],
        status="claimed" if resume_status == "consumed" else resume_status,
    )
    with Session(get_engine(settings)) as session:
        session.add(
            AgentExecutionAttempt(
                id=attempt_id,
                execution_id=execution_id,
                ordinal=1,
                worker_id=worker_id,
            )
        )
        session.flush()
        execution = session.get(AgentExecution, execution_id)
        resume = session.get(ExecutionResumeRequest, f"resume-{execution_id}")
        assert execution is not None and resume is not None
        execution.current_attempt_id = attempt_id
        if resume_status == "consumed":
            resume.status = "consumed"
            resume.consumed_at = utcnow()
            session.add(resume)
        session.add(execution)
        session.commit()

    with Session(get_engine(settings)) as session:
        execution = session.exec(
            select(AgentExecution)
            .where(AgentExecution.id == execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        assert settle_execution_failure(
            session,
            execution,
            now=current_database_time(session),
            error="resume graph failed",
            expected_worker_id=worker_id,
            expected_attempt_id=attempt_id,
        )
        session.commit()

    with Session(get_engine(settings)) as session:
        execution = session.exec(
            select(AgentExecution)
            .where(AgentExecution.id == execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        assert not settle_execution_failure(
            session,
            execution,
            now=current_database_time(session),
            error="must not overwrite terminal state",
            expected_worker_id=worker_id,
            expected_attempt_id=attempt_id,
        )
        session.rollback()
        attempt = session.get(AgentExecutionAttempt, attempt_id)
        resume = session.get(ExecutionResumeRequest, f"resume-{execution_id}")
        assert execution.status == RunStatus.failed
        assert execution.error == "resume graph failed"
        assert execution.streaming_degraded is True
        assert execution.streaming_degraded_reason == "TERMINAL_STREAM_NOT_PUBLISHED"
        assert attempt is not None
        assert attempt.status == ExecutionAttemptStatus.failed
        assert attempt.finished_at is not None
        assert resume is not None
        assert resume.status == expected_status


def test_direct_dispatcher_pins_claimed_checkpoint_through_runner_to_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    execution_id = "execution-direct-checkpoint-pinning"
    interrupt_id = "interrupt-direct-checkpoint-pinning"
    tool_calls = [
        {
            "name": "remember",
            "args": {"content": "pin this exact checkpoint"},
        }
    ]
    interrupt = {"id": interrupt_id, "value": {"tool_calls": tool_calls}}
    _seed_execution(settings, execution_id=execution_id)
    _seed_execute_outbox(settings, execution_id)
    _seed_resume_request(
        settings,
        execution_id,
        interrupt_id=interrupt_id,
        tool_calls=tool_calls,
    )
    checkpointer = _SequenceCheckpointer(
        [
            _checkpoint_tuple("checkpoint-A", interrupts=[interrupt]),
            _checkpoint_tuple("checkpoint-B"),
        ]
    )

    class RecordingGraph:
        def __init__(self) -> None:
            self.inputs: list[Any] = []
            self.configurables: list[dict[str, Any]] = []

        def stream(self, graph_input: Any, *, config: dict[str, Any], **_kwargs: Any):
            checkpointer.checkpoints = [_checkpoint_tuple("checkpoint-B")]
            self.inputs.append(graph_input)
            self.configurables.append(dict(config["configurable"]))
            yield (
                "updates",
                {
                    "__interrupt__": [
                        SimpleNamespace(
                            id="next-interrupt",
                            value={"tool_calls": []},
                        )
                    ]
                },
            )

    class FakeGateway:
        def build_hotspot_filter_model(self) -> None:
            return None

    class FakeContainer:
        def __init__(self) -> None:
            self.settings = settings
            self.tool_registry = SimpleNamespace(registrations=[])
            self.side_effect_dispatcher = None
            self.side_effect_receipt_poller = None
            self.graph = RecordingGraph()
            self.shared_config: dict[str, Any] | None = None

        def get_checkpointer(self) -> _SequenceCheckpointer:
            return checkpointer

        def create_runtime(self, **kwargs: Any) -> SimpleNamespace:
            self.shared_config = {
                "configurable": {
                    "thread_id": kwargs["session_id"],
                    "checkpoint_ns": "",
                    "execution_id": kwargs["execution_id"],
                },
                "recursion_limit": settings.agent.recursion_limit,
            }
            return SimpleNamespace(
                graph=self.graph,
                gateway=FakeGateway(),
                checkpointer=checkpointer,
                tool_permissions=("prepare_topic_research",),
                config=self.shared_config,
            )

    class RecordingWriter:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, Any]]] = []

        def emit(self, name: str, payload: dict[str, Any]) -> None:
            self.events.append((name, payload))

        def close(self) -> None:
            return None

    container = FakeContainer()
    runner = AgentRunner(container)
    writer = RecordingWriter()
    monkeypatch.setattr(runner.event_service, "new_writer", lambda *_args, **_kwargs: writer)
    service = SimpleNamespace(settings=settings, runtime=container, runner=runner)

    DirectDispatcher(service).dispatch(
        execution_id,
        request_id="request-direct-checkpoint",
    )

    assert checkpointer.calls == 1
    assert checkpointer.checkpoints[0].config["configurable"]["checkpoint_id"] == (
        "checkpoint-B"
    )
    assert len(container.graph.inputs) == 1
    graph_input = container.graph.inputs[0]
    assert isinstance(graph_input, Command)
    assert graph_input.resume == {"decision": "approve"}
    assert container.graph.configurables[0]["checkpoint_id"] == "checkpoint-A"
    assert container.shared_config is not None
    assert "checkpoint_id" not in container.shared_config["configurable"]
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.status == RunStatus.waiting_input
        assert execution.attempt_count == 1


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


def test_recovery_processes_expired_leases_in_bounded_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(max_execution_attempts=3)
    expired_at = utcnow() - timedelta(seconds=10)
    execution_ids = [f"execution-expired-batch-{index}" for index in range(3)]
    for execution_id in execution_ids:
        _seed_execution(
            settings,
            execution_id=execution_id,
            status=RunStatus.running,
            attempt_count=1,
            lease_expires_at=expired_at,
            worker_id=f"worker-{execution_id}",
        )
        with Session(get_engine(settings)) as session:
            session.add(
                ExecutionOutbox(
                    id=f"outbox-{execution_id}",
                    execution_id=execution_id,
                    model_config_id=DEFAULT_MODEL_CONFIG_ID,
                    status="published",
                    published_at=expired_at,
                )
            )
            session.commit()

    monkeypatch.setattr(tasks_module, "RECOVERY_BATCH_LIMIT", 2)
    monkeypatch.setattr(tasks_module, "get_settings", lambda: settings)

    assert tasks_module.recover_expired_executions.run() == 2
    with Session(get_engine(settings)) as session:
        statuses_after_first_batch = {
            execution.id: execution.status
            for execution in session.exec(
                select(AgentExecution).where(AgentExecution.id.in_(execution_ids))
            ).all()
        }
    assert statuses_after_first_batch == {
        execution_ids[0]: RunStatus.pending,
        execution_ids[1]: RunStatus.pending,
        execution_ids[2]: RunStatus.running,
    }

    assert tasks_module.recover_expired_executions.run() == 1
    with Session(get_engine(settings)) as session:
        assert {
            execution.status
            for execution in session.exec(
                select(AgentExecution).where(AgentExecution.id.in_(execution_ids))
            ).all()
        } == {RunStatus.pending}


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


def test_attempt_settlement_ignores_attempt_owned_by_another_execution() -> None:
    settings = _settings()
    owner_id = "execution-attempt-owner"
    other_id = "execution-attempt-other"
    attempt_id = "attempt-owned-by-first-execution"
    _seed_execution(settings, execution_id=owner_id)
    _seed_execution(settings, execution_id=other_id)

    with Session(get_engine(settings)) as session:
        attempt = AgentExecutionAttempt(
            id=attempt_id,
            execution_id=owner_id,
            ordinal=1,
            worker_id="owner-worker",
        )
        session.add(attempt)
        session.commit()

        other = AgentExecution(
            id=other_id,
            invocation_id=f"invocation-{other_id}",
            session_id=f"session-{other_id}",
            agent_version_id=f"version-{other_id}",
            model_config_id=DEFAULT_MODEL_CONFIG_ID,
            current_attempt_id=attempt_id,
        )
        assert other not in session
        finish_current_attempt(
            session,
            other,
            ExecutionAttemptStatus.failed,
            now=utcnow(),
        )
        session.commit()

    with Session(get_engine(settings)) as session:
        persisted = session.get(AgentExecutionAttempt, attempt_id)
        assert persisted is not None
        assert persisted.status == ExecutionAttemptStatus.running
        assert persisted.finished_at is None


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
    _seed_recovery_resume_states(settings, execution_id)
    monkeypatch.setattr(tasks_module, "get_settings", lambda: settings)

    assert tasks_module.recover_expired_executions.run() == 0
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.status == RunStatus.failed
        assert execution.finished_at is not None
        assert execution.error == ("Execution retry limit exceeded after worker lease expiry.")
        assert session.get(ExecutionOutbox, "outbox-retry-limit") is None
    _assert_recovery_resume_states(settings, execution_id)


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
    _seed_recovery_resume_states(settings, execution_id)

    monkeypatch.setattr(tasks_module, "get_settings", lambda: settings)

    assert tasks_module.recover_expired_executions.run() == 0
    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.status == RunStatus.failed
        assert execution.finished_at is not None
        assert execution.error == "Agent worker did not claim the execution within 1 seconds."
    _assert_recovery_resume_states(settings, execution_id)


def test_recovery_missing_execute_outbox_stales_live_resume_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(max_execution_attempts=3)
    execution_id = "execution-expired-missing-outbox"
    _seed_execution(
        settings,
        execution_id=execution_id,
        status=RunStatus.running,
        attempt_count=1,
        lease_expires_at=utcnow() - timedelta(seconds=10),
        worker_id="worker-expired-missing-outbox",
    )
    _seed_recovery_resume_states(settings, execution_id)
    monkeypatch.setattr(tasks_module, "get_settings", lambda: settings)

    assert tasks_module.recover_expired_executions.run() == 0

    with Session(get_engine(settings)) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.status == RunStatus.failed
        assert execution.finished_at is not None
        assert execution.error == "TURN_CONTEXT_SNAPSHOT_INVALID"
    _assert_recovery_resume_states(settings, execution_id)


def test_unclaimed_recovery_uses_fresh_database_clock_after_lock_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    settings.agent.worker_claim_timeout_seconds = 1
    execution_id = "execution-unclaimed-lock-wait-clock"
    _seed_execution(settings, execution_id=execution_id)
    with Session(get_engine(settings)) as session:
        session.add(
            ExecutionOutbox(
                execution_id=execution_id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
                kind="execute",
                status="published",
                published_at=utcnow() - timedelta(minutes=1),
            )
        )
        session.commit()

    engine = get_engine(settings)
    candidate_query_started = threading.Event()

    def before_cursor_execute(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        normalized = statement.lower()
        if (
            "executionoutbox" in normalized
            and "agentexecution" in normalized
            and "for update" in normalized
        ):
            candidate_query_started.set()

    sqlalchemy_event.listen(engine, "before_cursor_execute", before_cursor_execute)
    monkeypatch.setattr(tasks_module, "get_settings", lambda: settings)
    lock_updated_at: datetime | None = None
    try:
        with Session(engine) as lock_session:
            lock_session.execute(text("LOCK TABLE agentexecution IN ACCESS EXCLUSIVE MODE"))
            with ThreadPoolExecutor(max_workers=1) as executor:
                recovery = executor.submit(tasks_module.recover_expired_executions.run)
                if not candidate_query_started.wait(timeout=3):
                    lock_session.rollback()
                    pytest.fail("recovery did not reach the locked candidate query")
                try:
                    lock_session.execute(
                        text(
                            "UPDATE agentexecution "
                            "SET updated_at = clock_timestamp() "
                            "WHERE id = :execution_id"
                        ),
                        {"execution_id": execution_id},
                    )
                    lock_updated_at = lock_session.execute(
                        text(
                            "SELECT updated_at FROM agentexecution "
                            "WHERE id = :execution_id"
                        ),
                        {"execution_id": execution_id},
                    ).scalar_one()
                    lock_session.commit()
                except Exception:
                    lock_session.rollback()
                    raise
                assert recovery.result(timeout=5) == 0
    finally:
        sqlalchemy_event.remove(engine, "before_cursor_execute", before_cursor_execute)

    assert lock_updated_at is not None
    with Session(engine) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.status == RunStatus.failed
        assert execution.updated_at >= lock_updated_at
        assert execution.finished_at is not None
        assert execution.finished_at >= lock_updated_at


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
