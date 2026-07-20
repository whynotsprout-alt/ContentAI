from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from core.config import Settings
from db.session import (
    calculate_connection_budget,
    engine_options_for_role,
    get_engine,
)
from models.base import utcnow
from models.chat import ServiceHeartbeat
from pydantic import ValidationError
from services.agent_service import AgentService
from services.dispatcher import OutboxDispatcher
from services.readiness import check_api_readiness
from services.service_heartbeat import (
    REQUIRED_WORKER_QUEUES,
    service_heartbeats_ready,
    service_instance_id,
    upsert_service_heartbeat,
)
from sqlalchemy import delete
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


def test_default_operational_topology_stays_within_the_single_host_budget() -> None:
    settings = _settings()

    budget = calculate_connection_budget(settings.database)

    assert budget.total_connections <= 67
    assert budget.total_connections < budget.safe_connection_limit


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
