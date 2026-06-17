from __future__ import annotations

from collections.abc import Iterator

from articleforgeai.core.config import get_settings
from articleforgeai.core.paths import ensure_runtime_dirs
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
    _remove_legacy_platform_column()
    SQLModel.metadata.create_all(engine)
    _ensure_pipeline_columns()
    _ensure_account_columns()


def _remove_legacy_platform_column() -> None:
    from articleforgeai.models.db import PipelineRun

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    source_table = ""
    if "pipelinerun_with_platform" in tables:
        source_table = "pipelinerun_with_platform"
    elif "pipelinerun" in tables:
        columns = {column["name"] for column in inspector.get_columns("pipelinerun")}
        if "platform_id" in columns:
            source_table = "pipelinerun"
    if not source_table:
        return

    keep_columns = [
        "id",
        "session_id",
        "account_id",
        "user_message",
        "status",
        "next_stage",
        "selected_topic_data",
        "pending_payload",
        "selected_topic_title",
        "run_dir",
        "error",
        "created_at",
        "updated_at",
    ]
    with engine.begin() as connection:
        for index_name in [
            "ix_pipelinerun_account_id",
            "ix_pipelinerun_platform_id",
            "ix_pipelinerun_session_id",
            "ix_pipelinerun_status",
        ]:
            connection.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
        if source_table == "pipelinerun":
            connection.execute(text("ALTER TABLE pipelinerun RENAME TO pipelinerun_with_platform"))
            source_table = "pipelinerun_with_platform"
        connection.execute(text("DROP TABLE IF EXISTS pipelinerun"))
        PipelineRun.__table__.create(connection)
        joined_columns = ", ".join(keep_columns)
        connection.execute(
            text(
                f"INSERT INTO pipelinerun ({joined_columns}) "
                f"SELECT {joined_columns} FROM {source_table}"
            )
        )
        connection.execute(text(f"DROP TABLE {source_table}"))


def _ensure_pipeline_columns() -> None:

    inspector = inspect(engine)
    if "pipelinerun" not in inspector.get_table_names():
        return

    existing_columns = {column["name"] for column in inspector.get_columns("pipelinerun")}
    migrations = []
    for name in ("selected_topic_data", "next_stage", "pending_payload"):
        if name in existing_columns:
            continue
        migrations.append(name)

    if not migrations:
        return

    with engine.begin() as connection:
        for name in migrations:
            connection.execute(text(f"ALTER TABLE pipelinerun ADD COLUMN {name} TEXT"))
        connection.execute(
            text(
                "UPDATE pipelinerun "
                "SET next_stage = 'analyze_intent' "
                "WHERE next_stage IS NULL OR next_stage = 'analyze'"
            )
        )


def _ensure_account_columns() -> None:

    inspector = inspect(engine)
    if "account" not in inspector.get_table_names():
        return

    existing_columns = {column["name"] for column in inspector.get_columns("account")}
    migrations = []
    for name in (
        "description",
        "audience",
        "preferred_directions",
        "boundaries",
        "viral_patterns",
        "style_prompt",
        "raw_profile",
        "created_at",
        "updated_at",
    ):
        if name in existing_columns:
            continue
        migrations.append(name)

    if not migrations:
        return

    with engine.begin() as connection:
        for name in migrations:
            col_type = "TIMESTAMP" if name in {"created_at", "updated_at"} else "TEXT"
            connection.execute(text(f"ALTER TABLE account ADD COLUMN {name} {col_type}"))


def get_session() -> Iterator[Session]:
    with Session(engine) as session:
        yield session
