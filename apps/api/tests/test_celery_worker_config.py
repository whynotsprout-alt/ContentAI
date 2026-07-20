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


def test_side_effect_tasks_use_dedicated_and_background_queues() -> None:
    assert celery_app.conf.task_routes["contentai.execute_side_effect"] == {
        "queue": "agent-side-effects"
    }
    assert celery_app.conf.task_routes["contentai.reconcile_side_effects"] == {
        "queue": "agent-background"
    }
    assert celery_app.conf.beat_schedule["reconcile-stale-side-effects"] == {
        "task": "contentai.reconcile_side_effects",
        "schedule": 30.0,
        "options": {"queue": "agent-background"},
    }
