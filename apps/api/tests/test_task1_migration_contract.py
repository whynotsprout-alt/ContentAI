from __future__ import annotations

import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from core.alembic import build_alembic_config
from db.session import get_engine
from models.base import utcnow
from models.schemas.auth import CurrentUserResponse
from models.schemas.base import InputSchemaBase
from models.schemas.chat import MessageListRequest
from pydantic import ValidationError
from services.conversation_service import ConversationService, InvalidCursorError
from services.pagination import CursorSigner, encode_cursor
from sqlalchemy import inspect, text

PROJECT_ROOT = Path(__file__).resolve().parents[3]
VERSIONS_DIR = PROJECT_ROOT / "apps" / "api" / "src" / "contentai_migrations" / "versions"

EXPECTED_BUSINESS_TABLES = {
    "adminauditlog",
    "agentexecution",
    "agentexecutionattempt",
    "agentinvocation",
    "agentprofile",
    "agentversion",
    "appuser",
    "authsession",
    "checkpointdeletionoutbox",
    "chatmessage",
    "chatsession",
    "executionoutbox",
    "executionresumerequest",
    "memoryrecord",
    "modelconfiguration",
    "modelusage",
    "researchpackage",
    "serviceheartbeat",
    "sideeffectreceipt",
    "toolexecution",
}
LANGGRAPH_OWNED_TABLES = {
    "checkpoint_blobs",
    "checkpoint_migrations",
    "checkpoint_writes",
    "checkpoints",
    "store",
    "store_migrations",
}


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


def test_release_version_and_single_fresh_revision():
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
        assert tomllib.load(stream)["project"]["version"] == "0.5.0-rc.1"

    revisions = sorted(VERSIONS_DIR.glob("*.py"))
    assert [revision.name for revision in revisions] == [
        "202607210001_v050_initial_schema.py"
    ]
    script = ScriptDirectory.from_config(build_alembic_config())
    assert script.get_heads() == ["202607210001"]
    revision = script.get_revision("202607210001")
    assert revision is not None
    assert revision.down_revision is None


def test_fresh_baseline_roundtrip_preserves_langgraph_owned_tables():
    config = build_alembic_config()
    engine = get_engine()
    before = set(inspect(engine).get_table_names())
    preserved = before & LANGGRAPH_OWNED_TABLES
    assert {
        "checkpoint_blobs",
        "checkpoint_migrations",
        "checkpoint_writes",
        "checkpoints",
    } <= preserved

    try:
        command.downgrade(config, "base")
        at_base = set(inspect(engine).get_table_names())
        assert EXPECTED_BUSINESS_TABLES.isdisjoint(at_base)
        assert preserved <= at_base
        command.upgrade(config, "head")
        at_head = set(inspect(engine).get_table_names())
        assert EXPECTED_BUSINESS_TABLES <= at_head
        assert preserved <= at_head
    finally:
        command.upgrade(config, "head")


def test_old_revision_stamp_fails_before_schema_changes():
    config = build_alembic_config()
    engine = get_engine()
    before = set(inspect(engine).get_table_names())
    with engine.begin() as connection:
        original_revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        connection.execute(text("UPDATE alembic_version SET version_num = '202607170002'"))
    try:
        with pytest.raises(CommandError, match="Can't locate revision identified by"):
            command.upgrade(config, "head")
        assert set(inspect(engine).get_table_names()) == before
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE alembic_version SET version_num = :revision"),
                {"revision": original_revision},
            )


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


def test_message_cursor_rejects_naive_timestamps_with_stable_error():
    cursor = "2026-07-17T10:00:00|msg_naive"
    with pytest.raises(ValidationError, match="timezone-aware"):
        MessageListRequest(cursor=cursor)
    with pytest.raises(InvalidCursorError, match="signature"):
        ConversationService._decode_cursor(cursor)

    aware = "2026-07-17T10:00:00+08:00|msg_aware"
    assert MessageListRequest(cursor=aware).cursor == aware
    signer = CursorSigner("task1-test-secret")
    signed = encode_cursor(
        datetime(2026, 7, 17, 10, tzinfo=UTC),
        "msg_aware",
        signer=signer,
    )
    parsed, message_id = ConversationService._decode_cursor(signed, signer=signer) or (None, None)
    assert parsed is not None and parsed.utcoffset() is not None
    assert message_id == "msg_aware"


def test_input_schema_rejects_naive_datetimes_recursively():
    class NestedDatetimeRequest(InputSchemaBase):
        payload: dict[str, Any]

    with pytest.raises(ValidationError, match="timezone-aware"):
        NestedDatetimeRequest(payload={"nested": [{"at": datetime(2026, 7, 17, 10)}]})

    aware = datetime.fromisoformat("2026-07-17T10:00:00+08:00")
    assert NestedDatetimeRequest(payload={"nested": [{"at": aware}]}).payload


def test_alembic_metadata_matches_upgraded_database():
    command.check(build_alembic_config())


def test_tenant_and_execution_lineage_constraints_are_database_enforced():
    inspector = inspect(get_engine())
    assert {"must_change_password", "temporary_password_expires_at"} <= _column_names(
        inspector, "appuser"
    )
    assert "session_id" in _column_names(inspector, "agentexecution")
    assert "execution_id" in _column_names(inspector, "chatmessage")
    assert "payload" in _column_names(inspector, "executionoutbox")
    assert "model_config_id" in _column_names(inspector, "agentexecution")
    assert "model_config_id" in _column_names(inspector, "executionoutbox")
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
        ("model_config_id",),
        "modelconfiguration",
        ("id",),
    ) in execution_fks
    assert (
        ("execution_id", "model_config_id"),
        "agentexecution",
        ("id", "model_config_id"),
    ) in _foreign_key_column_sets(inspector, "executionoutbox")
    assert (
        ("execution_id", "session_id", "agent_version_id"),
        "agentexecution",
        ("id", "session_id", "agent_version_id"),
    ) in _foreign_key_column_sets(inspector, "researchpackage")


def test_removed_token_table_and_operational_foundation_tables():
    tables = set(inspect(get_engine()).get_table_names())
    assert "useractiontoken" not in tables
    assert {"serviceheartbeat", "checkpointdeletionoutbox", "sideeffectreceipt"} <= tables
