from __future__ import annotations

import socket
from datetime import datetime, timedelta
from os import getpid
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlmodel import Session, select

from contentai.models.base import utcnow
from contentai.models.chat import ServiceHeartbeat

HEARTBEAT_INTERVAL_SECONDS = 10
HEARTBEAT_STALE_SECONDS = 30
REQUIRED_WORKER_QUEUES = (
    "agent-executions",
    "agent-background",
    "agent-side-effects",
)


def service_instance_id() -> str:
    return f"{socket.gethostname()}:{getpid()}"


def upsert_service_heartbeat(
    session: Session,
    *,
    service_name: str,
    instance_id: str | None = None,
    queue_name: str = "",
    status: str = "healthy",
    detail: dict[str, Any] | None = None,
    heartbeat_at: datetime | None = None,
) -> None:
    now = heartbeat_at or utcnow()
    values = {
        "service_name": service_name,
        "instance_id": instance_id or service_instance_id(),
        "queue_name": queue_name,
        "status": status,
        "detail": detail or {},
        "heartbeat_at": now,
        "updated_at": now,
    }
    statement = insert(ServiceHeartbeat).values(**values)
    session.exec(
        statement.on_conflict_do_update(
            constraint="ux_serviceheartbeat_service_instance_queue",
            set_={
                "status": statement.excluded.status,
                "detail": statement.excluded.detail,
                "heartbeat_at": statement.excluded.heartbeat_at,
                "updated_at": statement.excluded.updated_at,
            },
        )
    )


def service_heartbeats_ready(
    session: Session, *, now: datetime | None = None
) -> tuple[bool, dict[str, bool | list[str]]]:
    cutoff = (now or utcnow()) - timedelta(seconds=HEARTBEAT_STALE_SECONDS)

    def fresh(service_name: str, queue_name: str = "") -> bool:
        return (
            session.exec(
                select(ServiceHeartbeat.id)
                .where(ServiceHeartbeat.service_name == service_name)
                .where(ServiceHeartbeat.queue_name == queue_name)
                .where(ServiceHeartbeat.status == "healthy")
                .where(ServiceHeartbeat.heartbeat_at >= cutoff)
                .limit(1)
            ).first()
            is not None
        )

    dispatcher = fresh("dispatcher")
    workers_missing = [
        queue_name
        for queue_name in REQUIRED_WORKER_QUEUES
        if not fresh("worker", queue_name)
    ]
    checks: dict[str, bool | list[str]] = {
        "dispatcher": dispatcher,
        "workers_missing": workers_missing,
    }
    return dispatcher and not workers_missing, checks


__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "HEARTBEAT_STALE_SECONDS",
    "REQUIRED_WORKER_QUEUES",
    "service_heartbeats_ready",
    "service_instance_id",
    "upsert_service_heartbeat",
]
