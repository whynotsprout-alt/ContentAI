from __future__ import annotations

from datetime import timedelta
from functools import lru_cache
from typing import Any

from alembic.script import ScriptDirectory
from redis import Redis
from sqlalchemy import func, text
from sqlmodel import Session, select

from contentai.core.alembic import build_alembic_config
from contentai.core.config import Settings
from contentai.db.session import get_engine
from contentai.models.base import utcnow
from contentai.models.chat import AgentExecution, ExecutionOutbox
from contentai.models.enums import RunStatus
from contentai.services.service_heartbeat import service_heartbeats_ready


def check_api_readiness(settings: Settings) -> tuple[bool, dict[str, Any]]:
    services_ready = False
    checks: dict[str, Any] = {
        "database": False,
        "alembic_version": None,
        "alembic_head": None,
        "database_revision_current": False,
        "checkpoint": False,
        "checkpoint_tables": {},
        "redis": False,
        "queue": False,
        "services": {"dispatcher": False, "workers_missing": []},
        "outbox_pending": None,
        "outbox_unclaimed_published": None,
        "outbox_oldest_age_seconds": None,
        "outbox_within_threshold": False,
    }
    try:
        checks["alembic_head"] = expected_alembic_head()
    except Exception as exc:  # noqa: BLE001
        checks["migration_error"] = exc.__class__.__name__
    try:
        with Session(get_engine(settings)) as session:
            session.exec(text("SELECT 1")).one()
            pending = session.exec(
                select(func.count())
                .select_from(ExecutionOutbox)
                .where(ExecutionOutbox.status == "pending")
            ).one()
            unclaimed_published = session.exec(
                select(func.count())
                .select_from(ExecutionOutbox)
                .join(AgentExecution, AgentExecution.id == ExecutionOutbox.execution_id)
                .where(ExecutionOutbox.status == "published")
                .where(ExecutionOutbox.kind == "execute")
                .where(ExecutionOutbox.published_at.is_not(None))
                .where(AgentExecution.status == RunStatus.pending)
                .where(AgentExecution.claimed_at.is_(None))
            ).one()
            oldest = session.exec(
                select(func.min(ExecutionOutbox.published_at))
                .join(AgentExecution, AgentExecution.id == ExecutionOutbox.execution_id)
                .where(ExecutionOutbox.status == "published")
                .where(ExecutionOutbox.kind == "execute")
                .where(ExecutionOutbox.published_at.is_not(None))
                .where(AgentExecution.status == RunStatus.pending)
                .where(AgentExecution.claimed_at.is_(None))
            ).one()
            alembic_version = session.exec(text("SELECT version_num FROM alembic_version")).one()[0]
            checkpoint_tables = {
                name: bool(
                    session.exec(
                        text(f"SELECT to_regclass('public.{name}') IS NOT NULL")
                    ).one()[0]
                )
                for name in (
                    "checkpoint_migrations",
                    "checkpoints",
                    "checkpoint_blobs",
                    "checkpoint_writes",
                )
            }
            services_ready, service_checks = service_heartbeats_ready(session)
        checks["database"] = True
        checks["alembic_version"] = str(alembic_version)
        checks["database_revision_current"] = str(alembic_version) == checks["alembic_head"]
        checks["checkpoint_tables"] = checkpoint_tables
        checks["checkpoint"] = all(checkpoint_tables.values())
        checks["services"] = service_checks
        checks["outbox_pending"] = int(pending)
        checks["outbox_unclaimed_published"] = int(unclaimed_published)
        oldest_age = utcnow() - oldest if oldest else timedelta()
        checks["outbox_oldest_age_seconds"] = max(0, int(oldest_age.total_seconds()))
        checks["outbox_within_threshold"] = not unclaimed_published or (
            oldest_age <= timedelta(seconds=settings.server.outbox_max_age_seconds)
        )
    except Exception as exc:  # noqa: BLE001
        checks["database_error"] = exc.__class__.__name__

    if settings.env.value == "test":
        checks["redis"] = True
    else:
        try:
            client = Redis.from_url(
                settings.redis.url,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
            )
            checks["redis"] = bool(client.ping())
        except Exception as exc:  # noqa: BLE001
            checks["redis_error"] = exc.__class__.__name__
    checks["queue"] = bool(checks["redis"] and services_ready)

    ready = bool(
        checks["database"]
        and checks["database_revision_current"]
        and checks["redis"]
        and checks["queue"]
        and not checks["services"]["workers_missing"]
        and checks["services"]["dispatcher"]
        and checks["checkpoint"]
        and checks["outbox_within_threshold"]
    )
    return ready, checks


@lru_cache(maxsize=1)
def expected_alembic_head() -> str:
    config = build_alembic_config()
    return str(ScriptDirectory.from_config(config).get_current_head())


__all__ = ["check_api_readiness", "expected_alembic_head"]
