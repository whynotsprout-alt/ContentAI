from __future__ import annotations

from functools import lru_cache
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from core.config import Env, Settings
from core.paths import PROJECT_ROOT
from db.session import get_engine
from models.base import utcnow
from models.chat import ExecutionOutbox
from redis import Redis
from sqlalchemy import func, text
from sqlmodel import Session, select


def check_api_readiness(settings: Settings) -> tuple[bool, dict[str, Any]]:
    checks: dict[str, Any] = {
        "database": False,
        "alembic_version": None,
        "alembic_head": None,
        "database_revision_current": False,
        "checkpoint": False,
        "redis": False,
        "queue": False,
        "outbox_pending": None,
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
            oldest = session.exec(
                select(func.min(ExecutionOutbox.created_at)).where(
                    ExecutionOutbox.status.in_(["pending", "publishing"])
                )
            ).one()
            alembic_version = session.exec(text("SELECT version_num FROM alembic_version")).one()[0]
            checkpoint_ready = session.exec(
                text("SELECT to_regclass('public.checkpoints') IS NOT NULL")
            ).one()[0]
        checks["database"] = True
        checks["alembic_version"] = str(alembic_version)
        checks["database_revision_current"] = str(alembic_version) == checks["alembic_head"]
        checks["checkpoint"] = bool(checkpoint_ready)
        checks["outbox_pending"] = int(pending)
        oldest_age = max(0, int((utcnow() - oldest).total_seconds())) if oldest else 0
        checks["outbox_oldest_age_seconds"] = oldest_age
        checks["outbox_within_threshold"] = (
            int(pending) <= settings.server.outbox_readiness_threshold
            and oldest_age <= settings.server.outbox_max_age_seconds
        )
    except Exception as exc:  # noqa: BLE001
        checks["database_error"] = exc.__class__.__name__

    if settings.env == Env.test:
        checks["redis"] = True
        checks["queue"] = True
    else:
        try:
            client = Redis.from_url(
                settings.redis.url,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
            )
            checks["redis"] = bool(client.ping())
            checks["queue"] = checks["redis"]
        except Exception as exc:  # noqa: BLE001
            checks["redis_error"] = exc.__class__.__name__

    ready = bool(
        checks["database"]
        and checks["database_revision_current"]
        and checks["redis"]
        and checks["queue"]
        and checks["checkpoint"]
        and checks["outbox_within_threshold"]
    )
    return ready, checks


@lru_cache(maxsize=1)
def expected_alembic_head() -> str:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    return str(ScriptDirectory.from_config(config).get_current_head())


__all__ = ["check_api_readiness", "expected_alembic_head"]
