from __future__ import annotations

import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
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


def _seed_v043_lineage(connection) -> None:
    statements = (
        """
        INSERT INTO appuser (
            id, email, email_normalized, password_hash, role, status,
            failed_login_count, created_at, updated_at, password_changed_at
        ) VALUES (
            'preflight-other-user', 'other@test.invalid', 'other@test.invalid',
            'hash', 'user', 'active', 0, now(), now(), now()
        )
        """,
        """
        INSERT INTO agentprofile (id, user_id, name, description, created_at, updated_at)
        VALUES ('preflight-other-agent', 'preflight-other-user', 'Other', '', now(), now())
        """,
        """
        INSERT INTO agentversion (
            id, agent_id, version, topic_scoring_prompt, content_prompt,
            hotspot_sources, created_at
        ) VALUES (
            'preflight-other-version', 'preflight-other-agent', 1, '', '', '[]', now()
        )
        """,
        """
        INSERT INTO chatsession (
            id, title, agent_id, agent_version_id, langgraph_thread_id,
            user_id, created_at, updated_at
        ) VALUES
            ('preflight-session', '', 'default-agent', 'default-agent-v1',
             'preflight-thread', 'local-user', now(), now()),
            ('preflight-other-session', '', 'preflight-other-agent',
             'preflight-other-version', 'preflight-other-thread',
             'preflight-other-user', now(), now())
        """,
        """
        INSERT INTO agentinvocation (id, session_id, agent_id, user_id, created_at)
        VALUES ('preflight-invocation', 'preflight-session', 'default-agent',
                'local-user', now())
        """,
    )
    for statement in statements:
        connection.execute(text(statement))


def _insert_v043_execution(connection, execution_id: str = "preflight-execution") -> None:
    connection.execute(
        text(
            """
            INSERT INTO agentexecution (
                id, invocation_id, agent_version_id, trace_id, status, error,
                interrupt_payload, resume_payload, attempt_count, next_attempt_kind,
                streaming_degraded, streaming_degraded_reason, created_at, updated_at
            ) VALUES (
                :execution_id, 'preflight-invocation', 'default-agent-v1',
                :trace_id, 'pending', '', '{}', '{}', 0, 'initial', false, '', now(), now()
            )
            """
        ),
        {"execution_id": execution_id, "trace_id": f"trace-{execution_id}"},
    )


PREFLIGHT_NEGATIVE_CASES = (
    (
        "owner",
        "preflight-session",
        "chatsession",
        ("UPDATE chatsession SET user_id = 'preflight-other-user' "
         "WHERE id = 'preflight-session'",),
    ),
    (
        "version",
        "preflight-session",
        "chatsession",
        ("UPDATE chatsession SET agent_version_id = 'preflight-other-version' "
         "WHERE id = 'preflight-session'",),
    ),
    (
        "invocation",
        "preflight-invocation",
        "agentinvocation",
        ("UPDATE agentinvocation SET agent_id = 'preflight-other-agent' "
         "WHERE id = 'preflight-invocation'",),
    ),
    (
        "execution",
        "preflight-execution",
        "agentexecution",
        (
            "INSERT INTO agentexecution (id, invocation_id, agent_version_id, trace_id, "
            "status, error, interrupt_payload, resume_payload, attempt_count, "
            "next_attempt_kind, streaming_degraded, streaming_degraded_reason, "
            "created_at, updated_at) VALUES ('preflight-execution', "
            "'preflight-invocation', 'preflight-other-version', 'trace-bad', "
            "'pending', '', '{}', '{}', 0, 'initial', false, '', now(), now())",
        ),
    ),
    (
        "memory",
        "preflight-memory",
        "memoryrecord",
        (
            "INSERT INTO memoryrecord (id, user_id, agent_id, memory_key, kind, payload, "
            "content, confidence, importance_score, source_type, version, access_count, "
            "created_at, updated_at) VALUES ('preflight-memory', 'preflight-other-user', "
            "'default-agent', 'bad-owner', 'semantic', '{}', '', 1, 0, 'manual', "
            "1, 0, now(), now())",
        ),
    ),
    (
        "research",
        "preflight-research",
        "researchpackage",
        (
            "__INSERT_EXECUTION__",
            "INSERT INTO researchpackage (id, session_id, execution_id, agent_version_id, "
            "topic, topic_hash, package_data, sources, provider_diagnostics, "
            "rendered_content, valid_source_count, isolated_source_count, "
            "removed_unknown_reference_count, created_at, updated_at) VALUES "
            "('preflight-research', 'preflight-other-session', 'preflight-execution', "
            "'preflight-other-version', '', 'topic', '{}', '[]', '{}', '', 0, 0, 0, "
            "now(), now())",
        ),
    ),
    (
        "multiple-executions",
        "preflight-execution-2",
        "agentexecution",
        ("__INSERT_EXECUTION__", "__INSERT_SECOND_EXECUTION__"),
    ),
    (
        "ambiguous-assistant",
        "preflight-message",
        "chatmessage",
        (
            "INSERT INTO chatmessage (id, session_id, invocation_id, role, message_type, "
            "content, created_at) VALUES ('preflight-message', 'preflight-session', "
            "'preflight-invocation', 'assistant', 'text', 'bad', now())",
        ),
    ),
    (
        "duplicate-assistant",
        "preflight-message-2",
        "chatmessage",
        (
            "__INSERT_EXECUTION__",
            "INSERT INTO chatmessage (id, session_id, invocation_id, role, message_type, "
            "content, created_at) VALUES "
            "('preflight-message-1', 'preflight-session', 'preflight-invocation', "
            "'assistant', 'text', 'one', now()), "
            "('preflight-message-2', 'preflight-session', 'preflight-invocation', "
            "'assistant', 'text', 'two', now())",
        ),
    ),
    (
        "nonempty-token",
        "preflight-token",
        "useractiontoken",
        (
            "INSERT INTO useractiontoken (id, user_id, purpose, token_hash, expires_at, "
            "created_at) VALUES ('preflight-token', 'local-user', 'reset', 'hash', "
            "now(), now())",
        ),
    ),
)


def _clean_v043_preflight_rows(connection) -> None:
    connection.execute(
        text(
            "TRUNCATE researchpackage, chatmessage, agentexecution, memoryrecord, "
            "agentinvocation, chatsession, useractiontoken CASCADE"
        )
    )
    connection.execute(
        text("DELETE FROM agentversion WHERE id = 'preflight-other-version'")
    )
    connection.execute(
        text("DELETE FROM agentprofile WHERE id = 'preflight-other-agent'")
    )
    connection.execute(text("DELETE FROM appuser WHERE id = 'preflight-other-user'"))


def test_v043_preflight_rejects_lineage_negatives_before_schema_changes():
    engine = get_engine()
    config = build_alembic_config()
    command.downgrade(config, "202607150001")
    try:
        for _case_name, offender_id, table_name, statements in PREFLIGHT_NEGATIVE_CASES:
            with engine.begin() as connection:
                _seed_v043_lineage(connection)
                for statement in statements:
                    if statement == "__INSERT_EXECUTION__":
                        _insert_v043_execution(connection)
                    elif statement == "__INSERT_SECOND_EXECUTION__":
                        _insert_v043_execution(connection, "preflight-execution-2")
                    else:
                        connection.execute(text(statement))
                before = connection.execute(
                    text(
                        f"SELECT to_jsonb(row_data) FROM {table_name} row_data "
                        "WHERE id = :id"
                    ),
                    {"id": offender_id},
                ).scalar_one()

            with pytest.raises(Exception, match=offender_id):
                command.upgrade(config, "head")

            with engine.connect() as connection:
                after = connection.execute(
                    text(
                        f"SELECT to_jsonb(row_data) FROM {table_name} row_data "
                        "WHERE id = :id"
                    ),
                    {"id": offender_id},
                ).scalar_one()
                assert after == before
                assert "must_change_password" not in _column_names(
                    inspect(connection), "appuser"
                )
            with engine.begin() as connection:
                _clean_v043_preflight_rows(connection)
    finally:
        with engine.begin() as connection:
            _clean_v043_preflight_rows(connection)
        command.upgrade(config, "head")


def test_expand_preflight_queries_are_orphan_safe():
    source = (VERSIONS_DIR / "202607170001_v050_expand_backfill.py").read_text(
        encoding="utf-8"
    )
    assert source.count("LEFT JOIN") >= 9
    assert source.count("IS DISTINCT FROM") >= 9
