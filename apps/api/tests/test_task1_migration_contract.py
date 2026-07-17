from __future__ import annotations

import tomllib
from datetime import UTC
from pathlib import Path

import pytest
from alembic import command
from core.alembic import build_alembic_config
from db.session import get_engine
from models.base import utcnow
from models.schemas.auth import CurrentUserResponse
from sqlalchemy import inspect, text

PROJECT_ROOT = Path(__file__).resolve().parents[3]
VERSIONS_DIR = PROJECT_ROOT / "apps" / "api" / "src" / "contentai_migrations" / "versions"


def _column_names(inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def _unique_column_sets(inspector, table: str) -> set[tuple[str, ...]]:
    constraints = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints(table)
    }
    constraints.update(
        tuple(index["column_names"])
        for index in inspector.get_indexes(table)
        if index.get("unique")
    )
    return constraints


def _foreign_key_column_sets(
    inspector, table: str
) -> set[tuple[tuple[str, ...], str, tuple[str, ...]]]:
    return {
        (
            tuple(constraint["constrained_columns"]),
            constraint["referred_table"],
            tuple(constraint["referred_columns"]),
        )
        for constraint in inspector.get_foreign_keys(table)
    }


def test_release_version_and_two_linear_revisions():
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
        assert tomllib.load(stream)["project"]["version"] == "0.5.0-rc.1"

    revisions = sorted(VERSIONS_DIR.glob("20260717*.py"))
    assert len(revisions) == 2
    expand = revisions[0].read_text(encoding="utf-8")
    contract = revisions[1].read_text(encoding="utf-8")
    assert '= "202607150001"' in expand
    assert f'= "{revisions[0].stem.split("_", 1)[0]}"' in contract
    assert "AT TIME ZONE 'UTC'" in expand
    assert "NOT VALID" in expand
    assert "VALIDATE CONSTRAINT" in contract


def test_utcnow_is_aware_and_api_datetimes_serialize_with_z():
    now = utcnow()
    assert now.tzinfo is UTC
    response = CurrentUserResponse(
        id="usr_time",
        email="time@example.com",
        role="user",
        status="active",
        email_verified_at=now,
        password_changed_at=now,
        created_at=now,
        last_login_at=now,
        must_change_password=True,
        temporary_password_expires_at=now,
    )
    payload = response.model_dump_json()
    assert "+00:00" not in payload
    assert payload.count("Z") == 5


def test_database_has_only_timezone_aware_application_timestamps():
    engine = get_engine()
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND data_type = 'timestamp without time zone'
                  AND table_name NOT IN (
                    'alembic_version', 'checkpoint_blobs', 'checkpoint_migrations',
                    'checkpoint_writes', 'checkpoints', 'store', 'store_migrations'
                  )
                ORDER BY table_name, ordinal_position
                """
            )
        ).all()
    assert rows == []


def test_tenant_and_execution_lineage_constraints_are_database_enforced():
    inspector = inspect(get_engine())
    assert {"must_change_password", "temporary_password_expires_at"} <= _column_names(
        inspector, "appuser"
    )
    assert "session_id" in _column_names(inspector, "agentexecution")
    assert "execution_id" in _column_names(inspector, "chatmessage")
    assert {"decision", "message_id"} <= _column_names(inspector, "executionresumerequest")

    assert ("id", "user_id") in _unique_column_sets(inspector, "agentprofile")
    assert ("id", "agent_id") in _unique_column_sets(inspector, "agentversion")
    assert ("id", "user_id") in _unique_column_sets(inspector, "chatsession")
    assert ("id", "agent_id", "user_id") in _unique_column_sets(inspector, "chatsession")
    assert ("id", "agent_version_id") in _unique_column_sets(inspector, "chatsession")
    assert ("id", "session_id") in _unique_column_sets(inspector, "agentinvocation")
    assert ("invocation_id",) in _unique_column_sets(inspector, "agentexecution")
    assert ("id", "session_id", "agent_version_id") in _unique_column_sets(
        inspector, "agentexecution"
    )

    session_fks = _foreign_key_column_sets(inspector, "chatsession")
    assert (("agent_id", "user_id"), "agentprofile", ("id", "user_id")) in session_fks
    assert (
        ("agent_version_id", "agent_id"),
        "agentversion",
        ("id", "agent_id"),
    ) in session_fks
    invocation_fks = _foreign_key_column_sets(inspector, "agentinvocation")
    assert (
        ("session_id", "agent_id", "user_id"),
        "chatsession",
        ("id", "agent_id", "user_id"),
    ) in invocation_fks
    execution_fks = _foreign_key_column_sets(inspector, "agentexecution")
    assert (
        ("invocation_id", "session_id"),
        "agentinvocation",
        ("id", "session_id"),
    ) in execution_fks
    assert (
        ("session_id", "agent_version_id"),
        "chatsession",
        ("id", "agent_version_id"),
    ) in execution_fks
    assert (
        ("execution_id", "session_id", "agent_version_id"),
        "agentexecution",
        ("id", "session_id", "agent_version_id"),
    ) in _foreign_key_column_sets(inspector, "researchpackage")


def test_removed_token_table_and_operational_foundation_tables():
    tables = set(inspect(get_engine()).get_table_names())
    assert "useractiontoken" not in tables
    assert {"serviceheartbeat", "checkpointdeletionoutbox", "sideeffectreceipt"} <= tables


def test_v043_preflight_aborts_and_names_non_empty_action_token_rows():
    engine = get_engine()
    config = build_alembic_config()
    command.downgrade(config, "202607150001")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO useractiontoken (
                        id, user_id, purpose, token_hash, expires_at, created_at
                    ) VALUES (
                        'uat_preflight_offender', 'local-user', 'reset',
                        'preflight-token-hash', now(), now()
                    )
                    """
                )
            )
        with pytest.raises(Exception, match="uat_preflight_offender"):
            command.upgrade(config, "head")
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM useractiontoken WHERE id = 'uat_preflight_offender'")
            )
        command.upgrade(config, "head")
