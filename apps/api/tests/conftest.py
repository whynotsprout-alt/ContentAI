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
    from agent.runtime.checkpoint import RuntimePersistence
    from core.config import get_settings

    persistence = RuntimePersistence(get_settings())
    try:
        persistence.setup()
    finally:
        persistence.close()


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
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))


@pytest.fixture(autouse=True)
def reset_database() -> None:
    from db.session import get_engine
    from sqlalchemy import text

    with get_engine().begin() as connection:
        connection.execute(
            text(
                """
                TRUNCATE TABLE
                    checkpoint_writes,
                    checkpoint_blobs,
                    checkpoints,
                    modelusage,
                    adminauditlog,
                    useractiontoken,
                    authsession,
                    appuser,
                    executionoutbox,
                    executionresumerequest,
                    agentevent,
                    toolexecution,
                    agentexecutionattempt,
                    researchpackage,
                    agentexecution,
                    agentinvocation,
                    chatmessage,
                    chatsession,
                    memoryrecord,
                    agentversion,
                    agentprofile
                RESTART IDENTITY CASCADE
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO appuser (
                    id, tenant_id, email, email_normalized, password_hash,
                    role, status, email_verified_at, password_changed_at,
                    failed_login_count, created_at, updated_at
                )
                VALUES (
                    'local-user', 'local', 'local@test.invalid', 'local@test.invalid',
                    'test-only-password-hash', 'user', 'active', now(), now(), 0, now(), now()
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO agentprofile (
                    id, tenant_id, owner_user_id, name, description, agent_type, status,
                    created_by_user_id, updated_by_user_id, created_at, updated_at
                )
                VALUES (
                    'default-agent',
                    'local',
                    'local-user',
                    'Default Agent',
                    'Default account used by tests.',
                    'content',
                    'active',
                    'local-user',
                    'local-user',
                    now(),
                    now()
                )
                ON CONFLICT (id) DO UPDATE
                SET tenant_id = EXCLUDED.tenant_id,
                    owner_user_id = EXCLUDED.owner_user_id,
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    agent_type = EXCLUDED.agent_type,
                    status = EXCLUDED.status,
                    updated_at = EXCLUDED.updated_at
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO agentversion (
                    id, agent_id, version, topic_scoring_prompt, content_prompt, graph_name,
                    tools_config,
                    memory_config, created_by_user_id, created_at
                )
                VALUES (
                    'default-agent-v1',
                    'default-agent',
                    1,
                    'Score test topics from 0 to 100.',
                    'Create concise test content.',
                    'default',
                    '{"hotspot_sources":["douyin","weibo"]}'::jsonb,
                    '{}'::jsonb,
                    'local-user',
                    now()
                )
                ON CONFLICT (id) DO UPDATE
                SET topic_scoring_prompt = EXCLUDED.topic_scoring_prompt,
                    content_prompt = EXCLUDED.content_prompt,
                    tools_config = EXCLUDED.tools_config
                """
            )
        )
