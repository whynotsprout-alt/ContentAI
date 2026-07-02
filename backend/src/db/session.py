from collections.abc import Iterator

from core.config import get_settings
from core.paths import ensure_runtime_dirs
from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine


def make_engine():
    settings = get_settings()
    connect_args = (
        {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
    )
    return create_engine(settings.database_url, connect_args=connect_args)


engine = make_engine()


def init_db() -> None:
    ensure_runtime_dirs()
    if _has_incompatible_schema():
        SQLModel.metadata.drop_all(engine)
    SQLModel.metadata.create_all(engine)


def check_db_connection() -> None:
    ensure_runtime_dirs()
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))


def _has_incompatible_schema() -> bool:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    required_columns = _required_column_map()

    for table_name, columns in required_columns.items():
        if table_name not in existing_tables:
            continue

        existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
        if existing_columns != columns:
            return True

    return False


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
    engine.dispose()
