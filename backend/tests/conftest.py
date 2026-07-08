import pytest

TEST_DATABASE_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test"
TEST_ENV_VARS = {
    "CONTENTAI_ENV": "test",
    "CONTENTAI_DATABASE__URL": TEST_DATABASE_URL,
    "CONTENTAI_SEARCH__TRAFFIC_RELAY_API_KEY": "test-key",
    "CONTENTAI_SEARCH__TRAFFIC_RELAY_BASE_URL": "https://example.test/v1",
}


def pytest_configure() -> None:
    import os

    from alembic import command
    from alembic.config import Config
    from core.config import set_settings_env_file
    from core.paths import PROJECT_ROOT

    test_env_file = PROJECT_ROOT / ".pytest_cache" / "contentai-test.env"
    test_env_file.parent.mkdir(parents=True, exist_ok=True)
    test_env_file.write_text(
        "\n".join(f"{name}={value}" for name, value in TEST_ENV_VARS.items()),
        encoding="utf-8",
    )
    os.environ.update(TEST_ENV_VARS)
    set_settings_env_file(test_env_file)
    _ensure_test_database_exists()
    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(alembic_config, "head")


def _ensure_test_database_exists() -> None:
    import psycopg
    from psycopg import sql
    from sqlalchemy.engine import make_url

    url = make_url(TEST_DATABASE_URL)
    database = url.database
    if not database:
        raise RuntimeError("CONTENTAI_DATABASE__URL must include a database name.")
    if database in {"postgres", "template0", "template1"}:
        raise RuntimeError("CONTENTAI_DATABASE__URL must point to a dedicated test database.")

    maintenance_url = (
        url.set(database="postgres")
        .render_as_string(hide_password=False)
        .replace("postgresql+psycopg://", "postgresql://", 1)
        .replace("postgresql+psycopg2://", "postgresql://", 1)
    )
    with psycopg.connect(maintenance_url, autocommit=True) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (database,),
        ).fetchone()
        if exists is None:
            connection.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
            )


@pytest.fixture(autouse=True)
def reset_database() -> None:
    from db.session import get_engine
    from sqlalchemy import text

    with get_engine().begin() as connection:
        connection.execute(
            text(
                """
                TRUNCATE TABLE
                    toolexecution,
                    agentexecution,
                    agentinvocation,
                    chatmessage,
                    chatsession,
                    memoryrecord,
                    account
                RESTART IDENTITY CASCADE
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO account (
                    id, tenant_id, name, positioning, topic_scoring_prompt,
                    content_creation_prompt, hotspot_sources, created_at, updated_at
                )
                VALUES (
                    'default-agent',
                    'local',
                    'Default Agent',
                    'Default account used by tests.',
                    'Score test topics from 0 to 100.',
                    'Create concise test content.',
                    '["douyin", "weibo"]'::jsonb,
                    now(),
                    now()
                )
                ON CONFLICT (id) DO UPDATE
                SET tenant_id = EXCLUDED.tenant_id,
                    name = EXCLUDED.name,
                    positioning = EXCLUDED.positioning,
                    topic_scoring_prompt = EXCLUDED.topic_scoring_prompt,
                    content_creation_prompt = EXCLUDED.content_creation_prompt,
                    hotspot_sources = EXCLUDED.hotspot_sources,
                    updated_at = EXCLUDED.updated_at
                """
            )
        )
