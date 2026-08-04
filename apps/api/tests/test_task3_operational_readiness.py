from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from contentai.core.config import Settings
from contentai.db.session import (
    calculate_connection_budget,
    engine_options_for_role,
    get_engine,
)
from contentai.models.base import utcnow
from contentai.models.chat import (
    AgentExecution,
    AgentInvocation,
    ChatSession,
    ExecutionOutbox,
    ServiceHeartbeat,
)
from contentai.models.enums import RunStatus
from contentai.services.agent_service import AgentService
from contentai.services.dispatcher import OutboxDispatcher
from contentai.services.readiness import check_api_readiness
from contentai.services.service_heartbeat import (
    REQUIRED_WORKER_QUEUES,
    service_heartbeats_ready,
    service_instance_id,
    upsert_service_heartbeat,
)
from model_config_helpers import DEFAULT_MODEL_CONFIG_ID
from pydantic import ValidationError
from sqlalchemy import delete, text
from sqlalchemy.pool import NullPool
from sqlmodel import Session, select


def _settings(**database: object) -> Settings:
    return Settings(
        env="test",
        database={
            "url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test",
            **database,
        },
    )


def _seed_ready_service_heartbeats(settings: Settings) -> None:
    with Session(get_engine(settings)) as session:
        upsert_service_heartbeat(
            session,
            service_name="dispatcher",
            instance_id="test:dispatcher",
        )
        for queue_name in REQUIRED_WORKER_QUEUES:
            upsert_service_heartbeat(
                session,
                service_name="worker",
                instance_id=f"test:{queue_name}",
                queue_name=queue_name,
            )
        session.commit()


def _seed_published_outbox(
    settings: Settings,
    *,
    suffix: str,
    published_at: datetime,
    claimed_at: datetime | None = None,
    status: str = "published",
    execution_status: RunStatus | None = None,
) -> None:
    with Session(get_engine(settings)) as session:
        chat = ChatSession(
            id=f"session-readiness-{suffix}",
            agent_id="default-agent",
            agent_version_id="default-agent-v1",
            user_id="local-user",
        )
        session.add(chat)
        session.flush()
        invocation = AgentInvocation(
            id=f"invocation-readiness-{suffix}",
            session_id=chat.id,
            agent_id=chat.agent_id,
            user_id=chat.user_id,
        )
        session.add(invocation)
        session.flush()
        execution = AgentExecution(
            id=f"execution-readiness-{suffix}",
            invocation_id=invocation.id,
            session_id=chat.id,
            agent_version_id=chat.agent_version_id,
            model_config_id=DEFAULT_MODEL_CONFIG_ID,
            claimed_at=claimed_at,
            status=execution_status or (RunStatus.completed if claimed_at else RunStatus.pending),
        )
        session.add(execution)
        session.flush()
        session.add(
            ExecutionOutbox(
                id=f"outbox-readiness-{suffix}",
                execution_id=execution.id,
                model_config_id=DEFAULT_MODEL_CONFIG_ID,
                kind="execute",
                status=status,
                published_at=published_at if status == "published" else None,
            )
        )
        session.commit()


def test_default_operational_topology_stays_within_the_single_host_budget() -> None:
    settings = _settings()

    budget = calculate_connection_budget(settings.database)

    assert budget.total_connections <= 67
    assert budget.total_connections < budget.safe_connection_limit


def test_default_outbox_readiness_age_is_thirty_seconds_and_declared_for_compose() -> None:
    project_root = Path(__file__).resolve().parents[3]
    env_example = (project_root / ".env.example").read_text(encoding="utf-8")
    compose = (project_root / "compose.yaml").read_text(encoding="utf-8")

    assert _settings().server.outbox_max_age_seconds == 30
    assert "CONTENTAI_SERVER__OUTBOX_MAX_AGE_SECONDS=30" in env_example
    assert "CONTENTAI_SERVER__OUTBOX_MAX_AGE_SECONDS" in compose


def test_connection_budget_counts_prefork_children_and_checkpoint_pool() -> None:
    settings = _settings()

    budget = calculate_connection_budget(settings.database)

    assert budget.by_role["agent-worker"] == 32
    assert budget.by_role["background-worker"] == 8
    assert budget.by_role["side-effect-worker"] == 4


def test_connection_budget_uses_the_configured_checkpoint_pool_size() -> None:
    settings = _settings(checkpoint_pool_size=3, max_connections=110)

    budget = calculate_connection_budget(settings.database)

    assert budget.by_role["agent-worker"] == 36


def test_non_graph_roles_do_not_eagerly_open_the_checkpoint_pool() -> None:
    settings = _settings(runtime_role="api")

    class RecordingRuntime:
        def __init__(self) -> None:
            self.settings = settings
            self.checkpointer_requested = False

        def get_checkpointer(self) -> object:
            self.checkpointer_requested = True
            return object()

    runtime = RecordingRuntime()
    service = AgentService(settings, runtime=runtime)  # type: ignore[arg-type]

    service.start()

    assert not runtime.checkpointer_requested


def test_settings_rejects_topology_that_exceeds_the_database_safety_limit() -> None:
    with pytest.raises(ValidationError, match="connection budget"):
        _settings(agent_worker_replicas=3)


def test_engine_options_follow_runtime_role_and_migration_uses_null_pool() -> None:
    settings = _settings(runtime_role="agent-worker")

    worker = engine_options_for_role(settings.database)
    migration = engine_options_for_role(settings.database, runtime_role="migration")

    assert worker["pool_size"] == 4
    assert worker["max_overflow"] == 2
    assert migration == {"poolclass": NullPool}


def test_compose_worker_concurrency_uses_the_budget_declarations() -> None:
    project_root = Path(__file__).resolve().parents[3]
    compose = (project_root / "compose.yaml").read_text(encoding="utf-8")
    env_example = (project_root / ".env.example").read_text(encoding="utf-8")

    for setting in (
        "API_REPLICAS",
        "DISPATCHER_REPLICAS",
        "AGENT_WORKER_REPLICAS",
        "BACKGROUND_WORKER_REPLICAS",
        "SIDE_EFFECT_WORKER_REPLICAS",
        "BEAT_REPLICAS",
        "MIGRATION_REPLICAS",
        "OPS_REPLICAS",
        "CHECKPOINT_POOL_SIZE",
    ):
        assert f"CONTENTAI_DATABASE__{setting}=" in env_example
    assert "--concurrency=${CONTENTAI_AGENT__WORKER_CONCURRENCY:-4}" in compose
    assert "--concurrency=${CONTENTAI_DATABASE__BACKGROUND_WORKER_CONCURRENCY:-2}" in compose
    assert "--concurrency=${CONTENTAI_DATABASE__SIDE_EFFECT_WORKER_CONCURRENCY:-1}" in compose


def test_service_heartbeat_upsert_keeps_one_row_per_runtime_identity() -> None:
    settings = _settings()
    now = utcnow()
    with Session(get_engine(settings)) as session:
        upsert_service_heartbeat(
            session,
            service_name="dispatcher",
            instance_id="host:1",
            heartbeat_at=now,
        )
        upsert_service_heartbeat(
            session,
            service_name="dispatcher",
            instance_id="host:1",
            heartbeat_at=now + timedelta(seconds=10),
        )
        session.commit()
        rows = session.exec(
            select(ServiceHeartbeat).where(ServiceHeartbeat.service_name == "dispatcher")
        ).all()

    assert len(rows) == 1
    assert rows[0].heartbeat_at == now + timedelta(seconds=10)


def test_required_service_heartbeats_are_missing_stale_and_then_ready() -> None:
    settings = _settings()
    now = utcnow()
    with Session(get_engine(settings)) as session:
        ready, checks = service_heartbeats_ready(session, now=now)
        assert not ready
        assert not checks["dispatcher"]
        assert set(checks["workers_missing"]) == set(REQUIRED_WORKER_QUEUES)

        upsert_service_heartbeat(
            session,
            service_name="dispatcher",
            instance_id="host:dispatcher",
            heartbeat_at=now - timedelta(seconds=31),
        )
        for queue_name in REQUIRED_WORKER_QUEUES:
            upsert_service_heartbeat(
                session,
                service_name="worker",
                instance_id=f"host:{queue_name}",
                queue_name=queue_name,
                heartbeat_at=now,
            )
        session.commit()
        ready, checks = service_heartbeats_ready(session, now=now)
        assert not ready
        assert not checks["dispatcher"]
        assert checks["workers_missing"] == []

        upsert_service_heartbeat(
            session,
            service_name="dispatcher",
            instance_id="host:dispatcher",
            heartbeat_at=now,
        )
        session.commit()
        ready, checks = service_heartbeats_ready(session, now=now)

    assert ready
    assert checks == {"dispatcher": True, "workers_missing": []}


def test_each_required_worker_queue_independently_blocks_readiness() -> None:
    settings = _settings()
    now = utcnow()
    with Session(get_engine(settings)) as session:
        upsert_service_heartbeat(
            session,
            service_name="dispatcher",
            instance_id="host:dispatcher",
            heartbeat_at=now,
        )
        for missing_queue in REQUIRED_WORKER_QUEUES:
            for queue_name in REQUIRED_WORKER_QUEUES:
                if queue_name != missing_queue:
                    upsert_service_heartbeat(
                        session,
                        service_name="worker",
                        instance_id=f"host:{queue_name}",
                        queue_name=queue_name,
                        heartbeat_at=now,
                    )
            session.commit()
            ready, checks = service_heartbeats_ready(session, now=now)
            assert not ready
            assert checks["workers_missing"] == [missing_queue]
            session.exec(delete(ServiceHeartbeat))
            session.commit()


def test_service_heartbeat_freshness_boundary_is_exactly_thirty_seconds() -> None:
    settings = _settings()
    now = utcnow()
    at_boundary = now - timedelta(seconds=30)
    with Session(get_engine(settings)) as session:
        upsert_service_heartbeat(
            session,
            service_name="dispatcher",
            instance_id="host:dispatcher",
            heartbeat_at=at_boundary,
        )
        for queue_name in REQUIRED_WORKER_QUEUES:
            upsert_service_heartbeat(
                session,
                service_name="worker",
                instance_id=f"host:{queue_name}",
                queue_name=queue_name,
                heartbeat_at=at_boundary,
            )
        session.commit()
        ready, _ = service_heartbeats_ready(session, now=now)
        assert ready

        upsert_service_heartbeat(
            session,
            service_name="dispatcher",
            instance_id="host:dispatcher",
            heartbeat_at=at_boundary - timedelta(microseconds=1),
        )
        session.commit()
        ready, checks = service_heartbeats_ready(session, now=now)

    assert not ready
    assert not checks["dispatcher"]


def test_readiness_requires_service_heartbeats_in_the_test_environment() -> None:
    ready, checks = check_api_readiness(_settings())

    assert not ready
    assert checks["services"] == {
        "dispatcher": False,
        "workers_missing": list(REQUIRED_WORKER_QUEUES),
    }


def test_readiness_recovers_after_all_required_heartbeats_are_restored() -> None:
    settings = _settings()

    ready, _ = check_api_readiness(settings)
    assert not ready

    _seed_ready_service_heartbeats(settings)
    ready, checks = check_api_readiness(settings)

    assert ready
    assert checks["services"] == {"dispatcher": True, "workers_missing": []}


def test_readiness_ignores_claimed_completed_published_outbox_history() -> None:
    settings = _settings()
    _seed_ready_service_heartbeats(settings)
    _seed_published_outbox(
        settings,
        suffix="claimed-history",
        published_at=utcnow() - timedelta(seconds=31),
        claimed_at=utcnow() - timedelta(seconds=31),
    )

    ready, checks = check_api_readiness(settings)

    assert ready
    assert checks["outbox_unclaimed_published"] == 0
    assert checks["outbox_within_threshold"]


def test_readiness_rejects_unclaimed_published_outbox_older_than_thirty_seconds() -> None:
    settings = _settings()
    _seed_ready_service_heartbeats(settings)
    _seed_published_outbox(
        settings,
        suffix="unclaimed-old",
        published_at=utcnow() - timedelta(seconds=31),
    )

    ready, checks = check_api_readiness(settings)

    assert not ready
    assert checks["outbox_unclaimed_published"] == 1
    assert checks["outbox_oldest_age_seconds"] > 30
    assert not checks["outbox_within_threshold"]


def test_readiness_ignores_published_outbox_for_failed_unclaimed_execution() -> None:
    settings = _settings()
    _seed_ready_service_heartbeats(settings)
    _seed_published_outbox(
        settings,
        suffix="failed-unclaimed",
        published_at=utcnow() - timedelta(minutes=5),
        execution_status=RunStatus.failed,
    )

    ready, checks = check_api_readiness(settings)

    assert ready
    assert checks["outbox_unclaimed_published"] == 0
    assert checks["outbox_within_threshold"]


def test_readiness_outbox_age_boundary_preserves_microseconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    now = utcnow()
    _seed_ready_service_heartbeats(settings)
    _seed_published_outbox(
        settings,
        suffix="age-boundary",
        published_at=now - timedelta(seconds=30),
    )
    monkeypatch.setattr("contentai.services.readiness.utcnow", lambda: now)

    ready, checks = check_api_readiness(settings)

    assert ready
    assert checks["outbox_oldest_age_seconds"] == 30

    with Session(get_engine(settings)) as session:
        outbox = session.get(ExecutionOutbox, "outbox-readiness-age-boundary")
        assert outbox is not None
        outbox.published_at = now - timedelta(seconds=30, microseconds=1)
        session.add(outbox)
        session.commit()

    ready, checks = check_api_readiness(settings)

    assert not ready
    assert checks["outbox_oldest_age_seconds"] == 30
    assert not checks["outbox_within_threshold"]


def test_readiness_ignores_pending_outbox_count_without_an_old_unclaimed_publish() -> None:
    settings = _settings()
    settings.server.outbox_readiness_threshold = 1
    _seed_ready_service_heartbeats(settings)
    _seed_published_outbox(
        settings,
        suffix="pending-one",
        published_at=utcnow(),
        status="pending",
    )
    _seed_published_outbox(
        settings,
        suffix="pending-two",
        published_at=utcnow(),
        status="pending",
    )

    ready, checks = check_api_readiness(settings)

    assert ready
    assert checks["outbox_pending"] == 2
    assert checks["outbox_unclaimed_published"] == 0
    assert checks["outbox_within_threshold"]


def test_readiness_reports_each_missing_checkpoint_table() -> None:
    settings = _settings()
    _seed_ready_service_heartbeats(settings)
    engine = get_engine(settings)

    for table_name in (
        "checkpoint_migrations",
        "checkpoints",
        "checkpoint_blobs",
        "checkpoint_writes",
    ):
        missing_name = f"{table_name}_readiness_missing"
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE {table_name} RENAME TO {missing_name}"))
        try:
            ready, checks = check_api_readiness(settings)
            assert not ready
            assert not checks["checkpoint_tables"][table_name]
            assert not checks["checkpoint"]
        finally:
            with engine.begin() as connection:
                connection.execute(text(f"ALTER TABLE {missing_name} RENAME TO {table_name}"))


def test_dispatcher_writes_a_service_heartbeat_even_when_outbox_is_empty() -> None:
    settings = _settings()

    assert OutboxDispatcher(settings, dispatcher_id="host:dispatcher").dispatch_once() == 0

    with Session(get_engine(settings)) as session:
        row = session.exec(
            select(ServiceHeartbeat)
            .where(ServiceHeartbeat.service_name == "dispatcher")
            .where(ServiceHeartbeat.instance_id == "host:dispatcher")
        ).one()

    assert row.queue_name == ""


def test_dispatcher_uses_stable_hostname_pid_identity_by_default() -> None:
    dispatcher = OutboxDispatcher(_settings())

    assert dispatcher.dispatcher_id == service_instance_id()
