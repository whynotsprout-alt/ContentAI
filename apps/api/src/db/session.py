from __future__ import annotations

from collections.abc import Iterator

from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from core.alembic import build_alembic_config
from core.config import Settings, get_settings
from core.config.database import (
    ConnectionBudget,
    pool_profile_for_role,
)
from core.config.database import (
    calculate_connection_budget as _calculate_connection_budget,
)
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool
from sqlmodel import Session, create_engine

type EngineFingerprint = tuple[str, str, float, int]

_engine: Engine | None = None
_engine_fingerprint: EngineFingerprint | None = None


def build_engine(settings: Settings) -> Engine:
    if settings.database.url is None:
        raise RuntimeError("Database URL is not configured.")
    options = engine_options_for_role(settings.database)
    return create_engine(
        settings.database.url,
        pool_timeout=settings.database.pool_timeout,
        pool_pre_ping=True,
        pool_recycle=settings.database.pool_recycle_seconds,
        **options,
    )


def engine_options_for_role(
    database_settings: object, *, runtime_role: str | None = None
) -> dict[str, int | type[NullPool]]:
    role = runtime_role or database_settings.runtime_role
    if role == "migration":
        return {"poolclass": NullPool}
    profile = pool_profile_for_role(role)
    return {"pool_size": profile.pool_size, "max_overflow": profile.max_overflow}


def calculate_connection_budget(database_settings: object) -> ConnectionBudget:
    return _calculate_connection_budget(database_settings)  # type: ignore[arg-type]


def get_engine(settings: Settings | None = None) -> Engine:
    global _engine, _engine_fingerprint

    resolved_settings = settings or get_settings()
    fingerprint = _settings_fingerprint(resolved_settings)
    if _engine is None or _engine_fingerprint != fingerprint:
        close_database()
        _engine = build_engine(resolved_settings)
        _engine_fingerprint = fingerprint
    return _engine


def init_database(settings: Settings | None = None) -> None:
    check_database_connection(settings)
    assert_database_at_alembic_head(settings)


def validate_database(settings: Settings | None = None) -> None:
    init_database(settings)


def check_database_connection(settings: Settings | None = None) -> None:
    with get_engine(settings).connect() as connection:
        connection.execute(text("SELECT 1"))


def assert_database_at_alembic_head(settings: Settings | None = None) -> None:
    alembic_config = build_alembic_config()
    script = ScriptDirectory.from_config(alembic_config)
    expected_heads = set(script.get_heads())
    if len(expected_heads) != 1:
        raise RuntimeError(
            "Alembic has multiple heads. Merge migration branches before starting "
            f"the application. Heads: {sorted(expected_heads)}."
        )

    with get_engine(settings).connect() as connection:
        context = MigrationContext.configure(connection)
        current_heads = set(context.get_current_heads())

    if current_heads != expected_heads:
        raise RuntimeError(
            "Database schema is not at the latest Alembic revision. "
            "Run `alembic upgrade head` before starting the application. "
            f"Current heads: {sorted(current_heads) or ['<none>']}; "
            f"expected heads: {sorted(expected_heads)}."
        )


def get_session() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session


def close_database() -> None:
    global _engine, _engine_fingerprint
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_fingerprint = None


def _settings_fingerprint(settings: Settings) -> EngineFingerprint:
    return (
        settings.database.url or "",
        settings.database.runtime_role,
        settings.database.pool_timeout,
        settings.database.pool_recycle_seconds,
    )
