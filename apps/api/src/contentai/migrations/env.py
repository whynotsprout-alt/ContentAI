from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, engine_from_config, pool, text
from sqlmodel import SQLModel

import contentai.models.database  # noqa: F401 - register SQLModel metadata
from contentai.core.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata
LANGGRAPH_OWNED_TABLES = {
    "checkpoint_blobs",
    "checkpoint_migrations",
    "checkpoint_writes",
    "checkpoints",
    "store",
    "store_migrations",
}
LEGACY_USAGE_BASELINE_REVISION = "202607210001"
LEGACY_USAGE_PRICING_REVISION = "202608030001"
LEGACY_USAGE_LOCK_SQL = "LOCK TABLE modelusage IN SHARE ROW EXCLUSIVE MODE"
LEGACY_USAGE_REPAIR_SQL = """
    UPDATE modelusage
    SET input_tokens = GREATEST(input_tokens, 0),
        output_tokens = GREATEST(output_tokens, 0),
        total_tokens = GREATEST(
            total_tokens,
            GREATEST(input_tokens, 0) + GREATEST(output_tokens, 0),
            0
        )
    WHERE input_tokens < 0
       OR output_tokens < 0
       OR total_tokens < 0
       OR total_tokens < (
            GREATEST(input_tokens, 0) + GREATEST(output_tokens, 0)
       )
"""


def _include_name(name: str | None, type_: str, _parent_names: dict[str, str]) -> bool:
    return not (type_ == "table" and name in LANGGRAPH_OWNED_TABLES)


def _include_object(
    _object,
    name: str | None,
    type_: str,
    _reflected: bool,
    _compare_to,
) -> bool:
    return not (type_ == "table" and name in LANGGRAPH_OWNED_TABLES)


def _database_url() -> str:
    database_url = get_settings().database.url
    if database_url is None:
        raise RuntimeError("Database URL is not configured.")
    return database_url


def _upgrade_path_includes_legacy_pricing() -> bool:
    migration_context = context.get_context()
    migration_function = migration_context.opts.get("fn")
    if getattr(migration_function, "__name__", None) != "upgrade":
        return False
    current_heads = migration_context.get_current_heads()
    if set(current_heads) != {LEGACY_USAGE_BASELINE_REVISION}:
        return False
    upgrade_steps = migration_function(current_heads, migration_context)
    return any(
        step.is_upgrade and LEGACY_USAGE_PRICING_REVISION in step.to_revisions
        for step in upgrade_steps
    )


def _repair_legacy_usage_before_pricing(connection: Connection) -> None:
    """Repair baseline token corruption before immutable revision 030001 runs."""

    if not _upgrade_path_includes_legacy_pricing():
        return

    # Hold writers out until all requested migrations commit. Otherwise an old
    # application process could reintroduce a negative token after this repair
    # but before 030001 installs its nonnegative cost constraints.
    connection.execute(text(LEGACY_USAGE_LOCK_SQL))
    result = connection.execute(text(LEGACY_USAGE_REPAIR_SQL))
    if result.rowcount:
        config.print_stdout(
            "Repaired %d legacy modelusage token row(s) before revision 202608030001.",
            result.rowcount,
        )


def _emit_offline_legacy_usage_repair() -> None:
    if not _upgrade_path_includes_legacy_pricing():
        return
    migration_context = context.get_context()
    migration_context.execute(text(LEGACY_USAGE_LOCK_SQL))
    migration_context.execute(text(LEGACY_USAGE_REPAIR_SQL))


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_name=_include_name,
        include_object=_include_object,
        compare_type=False,
    )

    with context.begin_transaction():
        _emit_offline_legacy_usage_repair()
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_name=_include_name,
            include_object=_include_object,
            include_schemas=False,
            compare_type=False,
        )
        with context.begin_transaction():
            _repair_legacy_usage_before_pricing(connection)
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
