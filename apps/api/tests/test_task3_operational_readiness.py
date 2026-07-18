from __future__ import annotations

from datetime import timedelta

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
from services.dispatcher import OutboxDispatcher
from services.service_heartbeat import (
    REQUIRED_WORKER_QUEUES,
    service_heartbeats_ready,
    upsert_service_heartbeat,
)
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
