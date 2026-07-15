from __future__ import annotations

from services.celery_app import celery_app, worker_pool_options


def test_windows_workers_use_solo_pool() -> None:
    assert worker_pool_options(platform_name="nt") == {
        "worker_pool": "solo",
        "worker_concurrency": 1,
    }


def test_posix_workers_keep_prefork_parallelism() -> None:
    options = worker_pool_options(platform_name="posix")

    assert options["worker_pool"] == "prefork"
    assert int(options["worker_concurrency"]) >= 1


def test_recovery_task_uses_background_queue() -> None:
    assert celery_app.conf.task_routes["contentai.recover_expired_executions"] == {
        "queue": "agent-background"
    }
