from __future__ import annotations

from collections.abc import Iterator

from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from core.alembic import build_alembic_config
from core.config import Settings, get_settings
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

type EngineFingerprint = tuple[str, int, int, float, int]

_engine: Engine | None = None
_engine_fingerprint: EngineFingerprint | None = None


def build_engine(settings: Settings) -> Engine:
    if settings.database.url is None:
        raise RuntimeError("Database URL is not configured.")
    return create_engine(
        settings.database.url,
        pool_size=settings.database.pool_size,
        max_overflow=settings.database.max_overflow,
        pool_timeout=settings.database.pool_timeout,
        pool_pre_ping=True,
        pool_recycle=settings.database.pool_recycle_seconds,
    )


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
        settings.database.pool_size,
        settings.database.max_overflow,
        settings.database.pool_timeout,
        settings.database.pool_recycle_seconds,
    )
