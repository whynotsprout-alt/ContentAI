"""Celery application for immutable execution identifiers and background jobs."""

import os

from celery import Celery
from celery.signals import setup_logging, worker_process_shutdown, worker_shutdown

from contentai.core.config import get_settings
from contentai.core.logging import configure_logging
from contentai.services.event_stream import close_cached_event_streams

settings = get_settings()


def worker_pool_options(*, platform_name: str | None = None) -> dict[str, int | str]:
    """Use a Windows-safe pool for native development workers.

    Celery's prefork pool can leave child processes permanently stuck on Windows.
    Containers and other POSIX deployments retain the configured parallelism.
    """
    platform = platform_name or os.name
    if platform == "nt":
        return {"worker_pool": "solo", "worker_concurrency": 1}
    return {
        "worker_pool": "prefork",
        "worker_concurrency": settings.agent.worker_concurrency,
    }


celery_app = Celery(
    "contentai",
    broker=settings.redis.url,
    backend=None,
    include=["contentai.services.tasks"],
)
celery_app.conf.update(
    task_default_queue=settings.agent.celery_queue,
    task_routes={
        "contentai.execute_agent": {"queue": settings.agent.celery_queue},
        # Keep the watchdog independent from the agent execution pool. It can
        # still expire a stalled run when every execution worker is unavailable.
        "contentai.recover_expired_executions": {"queue": settings.agent.celery_background_queue},
        "contentai.process_agent_post_execution": {"queue": settings.agent.celery_background_queue},
        "contentai.execute_side_effect": {"queue": settings.agent.celery_side_effect_queue},
        "contentai.reconcile_side_effects": {"queue": settings.agent.celery_background_queue},
        "contentai.record_queue_heartbeat": {"queue": settings.agent.celery_queue},
    },
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    **worker_pool_options(),
    task_track_started=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    broker_transport_options={
        "visibility_timeout": settings.agent.celery_visibility_timeout_seconds
    },
    task_soft_time_limit=settings.agent.worker_soft_time_limit_seconds,
    task_time_limit=settings.agent.worker_time_limit_seconds,
    beat_schedule={
        "recover-expired-agent-executions": {
            "task": "contentai.recover_expired_executions",
            "schedule": 30.0,
        },
        "reconcile-stale-side-effects": {
            "task": "contentai.reconcile_side_effects",
            "schedule": 30.0,
            "options": {"queue": settings.agent.celery_background_queue},
        },
        "heartbeat-agent-executions": {
            "task": "contentai.record_queue_heartbeat",
            "schedule": 10.0,
            "args": (settings.agent.celery_queue,),
            "options": {"queue": settings.agent.celery_queue},
        },
        "heartbeat-agent-background": {
            "task": "contentai.record_queue_heartbeat",
            "schedule": 10.0,
            "args": (settings.agent.celery_background_queue,),
            "options": {"queue": settings.agent.celery_background_queue},
        },
        "heartbeat-agent-side-effects": {
            "task": "contentai.record_queue_heartbeat",
            "schedule": 10.0,
            "args": (settings.agent.celery_side_effect_queue,),
            "options": {"queue": settings.agent.celery_side_effect_queue},
        },
    },
)


@setup_logging.connect
def configure_celery_logging(**_kwargs: object) -> None:
    import sys

    argv = " ".join(sys.argv)
    if " beat" in f" {argv}":
        service_name = "beat"
    elif "agent-background" in argv:
        service_name = "background-worker"
    else:
        service_name = "agent-worker"
    configure_logging(service_name, settings)


@worker_process_shutdown.connect
@worker_shutdown.connect
def close_worker_event_streams(**_kwargs: object) -> None:
    close_cached_event_streams()
