from collections.abc import Iterator

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from core.config import get_settings
from core.paths import PROJECT_ROOT, ensure_runtime_dirs
from sqlalchemy import inspect, text
from sqlmodel import Session, create_engine


def make_engine():
    settings = get_settings()
    return create_engine(settings.database_url)


engine = make_engine()


def init_db() -> None:
    ensure_runtime_dirs()
    check_db_connection()
    _assert_database_is_at_head()
    _assert_schema_has_required_columns()


def check_db_connection() -> None:
    ensure_runtime_dirs()
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))


def _assert_database_is_at_head() -> None:
    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
    script = ScriptDirectory.from_config(alembic_config)
    expected_heads = set(script.get_heads())
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        current_heads = set(context.get_current_heads())

    if current_heads != expected_heads:
        raise RuntimeError(
            "Database schema is not at the latest Alembic revision. "
            "Run `alembic upgrade head` before starting the application. "
            f"Current heads: {sorted(current_heads) or ['<none>']}; "
            f"expected heads: {sorted(expected_heads)}."
        )


def _assert_schema_has_required_columns() -> None:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    required_columns = _required_column_map()
    missing: dict[str, set[str]] = {}

    for table_name, columns in required_columns.items():
        if table_name not in existing_tables:
            continue

        existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
        missing_columns = columns - existing_columns
        if missing_columns:
            missing[table_name] = missing_columns

    if missing:
        detail = "; ".join(
            f"{table}: {', '.join(sorted(columns))}"
            for table, columns in sorted(missing.items())
        )
        raise RuntimeError(
            "Database schema is missing required columns. "
            "Run an Alembic migration before starting the application. "
            f"Missing columns: {detail}"
        )


def _required_column_map() -> dict[str, set[str]]:
    from models.db import (
        Account,
        AgentRun,
        AgentRunEvent,
        ChatMessage,
        ChatSession,
        MemoryRecord,
    )

    models = (
        Account,
        AgentRun,
        AgentRunEvent,
        ChatMessage,
        ChatSession,
        MemoryRecord,
    )
    return {model.__table__.name: set(model.__table__.columns.keys()) for model in models}


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session


def close_db() -> None:
    from agent.runtime.checkpoint import close_runtime_persistence

    close_runtime_persistence()
    engine.dispose()
