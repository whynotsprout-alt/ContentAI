from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

RUNTIME_ROLES = {
    "api",
    "dispatcher",
    "agent-worker",
    "background-worker",
    "side-effect-worker",
    "beat",
    "migration",
    "ops",
}


@dataclass(frozen=True)
class PoolProfile:
    pool_size: int
    max_overflow: int

    @property
    def capacity(self) -> int:
        return self.pool_size + self.max_overflow


ROLE_POOL_PROFILES: dict[str, PoolProfile] = {
    "api": PoolProfile(pool_size=7, max_overflow=3),
    "dispatcher": PoolProfile(pool_size=4, max_overflow=2),
    "agent-worker": PoolProfile(pool_size=4, max_overflow=2),
    "background-worker": PoolProfile(pool_size=3, max_overflow=1),
    "side-effect-worker": PoolProfile(pool_size=3, max_overflow=1),
    "beat": PoolProfile(pool_size=2, max_overflow=0),
    # Migration uses NullPool, but it can still consume one connection while running.
    "migration": PoolProfile(pool_size=1, max_overflow=0),
    "ops": PoolProfile(pool_size=1, max_overflow=0),
}


@dataclass(frozen=True)
class ConnectionBudget:
    by_role: dict[str, int]
    total_connections: int
    safe_connection_limit: float


class DatabaseSettings(BaseModel):
    url: str | None = None
    runtime_role: str = "api"
    max_connections: int = 100
    reserved_connections: int = 5
    api_replicas: int = 1
    dispatcher_replicas: int = 1
    agent_worker_replicas: int = 1
    agent_worker_concurrency: int = 4
    background_worker_replicas: int = 1
    background_worker_concurrency: int = 2
    side_effect_worker_replicas: int = 1
    side_effect_worker_concurrency: int = 1
    checkpoint_pool_size: int = 2
    beat_replicas: int = 1
    migration_replicas: int = 1
    ops_replicas: int = 1
    pool_timeout: float = 30.0
    pool_recycle_seconds: int = 600


def pool_profile_for_role(runtime_role: str) -> PoolProfile:
    try:
        return ROLE_POOL_PROFILES[runtime_role]
    except KeyError as exc:
        raise ValueError(f"Unknown runtime role: {runtime_role}.") from exc


def calculate_connection_budget(settings: DatabaseSettings) -> ConnectionBudget:
    replica_counts = {
        "api": settings.api_replicas,
        "dispatcher": settings.dispatcher_replicas,
        "agent-worker": settings.agent_worker_replicas * settings.agent_worker_concurrency,
        "background-worker": (
            settings.background_worker_replicas * settings.background_worker_concurrency
        ),
        "side-effect-worker": (
            settings.side_effect_worker_replicas * settings.side_effect_worker_concurrency
        ),
        "beat": settings.beat_replicas,
        "migration": settings.migration_replicas,
        "ops": settings.ops_replicas,
    }
    by_role = {
        role: (
            pool_profile_for_role(role).capacity
            + (settings.checkpoint_pool_size if role == "agent-worker" else 0)
        )
        * replicas
        for role, replicas in replica_counts.items()
    }
    safe_connection_limit = (settings.max_connections - settings.reserved_connections) * 0.70
    return ConnectionBudget(
        by_role=by_role,
        total_connections=sum(by_role.values()),
        safe_connection_limit=safe_connection_limit,
    )


def validate_connection_budget(settings: DatabaseSettings) -> None:
    if settings.runtime_role not in RUNTIME_ROLES:
        raise ValueError(
            "CONTENTAI_DATABASE__RUNTIME_ROLE must be one of: "
            f"{', '.join(sorted(RUNTIME_ROLES))}."
        )
    if settings.max_connections < 1:
        raise ValueError("CONTENTAI_DATABASE__MAX_CONNECTIONS must be at least 1.")
    if (
        settings.reserved_connections < 0
        or settings.reserved_connections >= settings.max_connections
    ):
        raise ValueError(
            "CONTENTAI_DATABASE__RESERVED_CONNECTIONS must be non-negative and below "
            "CONTENTAI_DATABASE__MAX_CONNECTIONS."
        )
    if settings.checkpoint_pool_size < 1:
        raise ValueError("CONTENTAI_DATABASE__CHECKPOINT_POOL_SIZE must be at least 1.")
    for field_name, value in settings.model_dump().items():
        if field_name.endswith(("_replicas", "_concurrency")) and value < 1:
            raise ValueError(f"CONTENTAI_DATABASE__{field_name.upper()} must be at least 1.")
    budget = calculate_connection_budget(settings)
    if budget.total_connections >= budget.safe_connection_limit:
        raise ValueError(
            "Configured database connection budget "
            f"({budget.total_connections}) must stay below 70% of available PostgreSQL "
            f"connections ({budget.safe_connection_limit:g})."
        )
