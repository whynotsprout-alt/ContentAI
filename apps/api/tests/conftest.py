import base64
import os
from pathlib import Path

import pytest
from model_config_helpers import (
    DEFAULT_MODEL_RUNTIME_PARAMETERS,
    TEST_MODEL_CONFIG_API_KEY,
    resolve_test_database_url,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]

TEST_DATABASE_URL = resolve_test_database_url()
TEST_MODEL_CONFIG_ENCRYPTION_KEY = base64.urlsafe_b64encode(bytes([7]) * 32).decode("ascii")
TEST_ENV_VARS = {
    "CONTENTAI_ENV": "test",
    "CONTENTAI_DATABASE__URL": TEST_DATABASE_URL,
    "CONTENTAI_SEARCH__TRAFFIC_RELAY_API_KEY": "test-key",
    "CONTENTAI_SEARCH__TRAFFIC_RELAY_BASE_URL": "https://example.test/v1",
    "CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY": TEST_MODEL_CONFIG_ENCRYPTION_KEY,
}


def pytest_configure() -> None:
    from alembic import command
    from contentai.core.alembic import build_alembic_config
    from contentai.core.config import set_settings_env_file

    worker_id = os.getenv("PYTEST_XDIST_WORKER", "master").strip() or "master"
    test_env_file = PROJECT_ROOT / ".pytest_cache" / f"contentai-test-{worker_id}.env"
    test_env_file.parent.mkdir(parents=True, exist_ok=True)
    test_env_file.write_text(
        "\n".join(f"{name}={value}" for name, value in TEST_ENV_VARS.items()),
        encoding="utf-8",
    )
    os.environ.update(TEST_ENV_VARS)
    set_settings_env_file(test_env_file)
    _ensure_test_database_exists()
    alembic_config = build_alembic_config()
    command.upgrade(alembic_config, "head")
    from contentai.agent.runtime.checkpoint import RuntimePersistence
    from contentai.core.config import get_settings

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
    if "test" not in database.casefold():
        raise RuntimeError(
            "Refusing to reset a database whose name does not contain the explicit "
            f"test marker: {database!r}."
        )

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
    from contentai.core.config import get_settings
    from contentai.core.model_config_crypto import ModelConfigurationSecretProtector
    from contentai.db.session import get_engine
    from sqlalchemy import text

    protector = ModelConfigurationSecretProtector(
        get_settings().model_config_encryption_key
    )
    api_key_ciphertext = protector.encrypt(TEST_MODEL_CONFIG_API_KEY)
    api_key_fingerprint = protector.fingerprint(TEST_MODEL_CONFIG_API_KEY)
    api_key_hint = protector.hint(TEST_MODEL_CONFIG_API_KEY)

    with get_engine().begin() as connection:
        connection.execute(
            text(
                """
                TRUNCATE TABLE
                    checkpoint_writes,
                    checkpoint_blobs,
                    checkpoints,
                    modelconfiguration,
                    modelusage,
                    adminauditlog,
                    authsession,
                    sideeffectreceipt,
                    checkpointdeletionoutbox,
                    serviceheartbeat,
                    executionoutbox,
                    executionresumerequest,
                    toolexecution,
                    agentexecutionattempt,
                    researchpackage,
                    agentexecution,
                    agentinvocation,
                    chatmessage,
                    chatsession,
                    memoryrecord,
                    agentversion,
                    agentprofile,
                    appuser
                RESTART IDENTITY CASCADE
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO appuser (
                    id, email, email_normalized, password_hash,
                    role, status, email_verified_at, password_changed_at, must_change_password,
                    failed_login_count, created_at, updated_at
                )
                VALUES (
                    'local-user', 'local@test.invalid', 'local@test.invalid',
                    'test-only-password-hash', 'user', 'active', now(), now(), false, 0,
                    now(), now()
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO modelconfiguration (
                    id, version, provider, base_url, model_name,
                    temperature, context_window_tokens,
                    chat_max_tokens, structured_max_tokens,
                    api_key_ciphertext, api_key_fingerprint, api_key_hint,
                    is_active, validated_at, created_at, superseded_at,
                    created_by_user_id
                )
                VALUES (
                    'default-model-config', 1, 'openai_compatible',
                    'https://models.test.invalid/v1', 'test-model',
                    :temperature, :context_window_tokens,
                    :chat_max_tokens, :structured_max_tokens,
                    :api_key_ciphertext, :api_key_fingerprint, :api_key_hint,
                    true, now(), now(), NULL, 'local-user'
                )
                """
            ),
            {
                "api_key_ciphertext": api_key_ciphertext,
                "api_key_fingerprint": api_key_fingerprint,
                "api_key_hint": api_key_hint,
                **DEFAULT_MODEL_RUNTIME_PARAMETERS,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO agentprofile (
                    id, user_id, name, description, created_at, updated_at
                )
                VALUES (
                    'default-agent',
                    'local-user',
                    'Default Agent',
                    'Default account used by tests.',
                    now(),
                    now()
                )
                ON CONFLICT (id) DO UPDATE
                SET name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    updated_at = EXCLUDED.updated_at
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO agentversion (
                    id, agent_id, version, topic_scoring_prompt, content_prompt,
                    hotspot_sources, created_at
                )
                VALUES (
                    'default-agent-v1',
                    'default-agent',
                    1,
                    'Score test topics from 0 to 100.',
                    'Create concise test content.',
                    '["douyin","weibo"]'::jsonb,
                    now()
                )
                ON CONFLICT (id) DO UPDATE
                SET topic_scoring_prompt = EXCLUDED.topic_scoring_prompt,
                    content_prompt = EXCLUDED.content_prompt,
                    hotspot_sources = EXCLUDED.hotspot_sources
                """
            )
        )
