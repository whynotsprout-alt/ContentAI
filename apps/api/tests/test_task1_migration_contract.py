from __future__ import annotations

import tomllib
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from contentai.core.alembic import build_alembic_config
from contentai.db.session import get_engine
from contentai.models.base import utcnow
from contentai.models.schemas.auth import CurrentUserResponse
from contentai.models.schemas.base import InputSchemaBase
from contentai.models.schemas.chat import MessageListRequest
from contentai.services.conversation_service import ConversationService, InvalidCursorError
from contentai.services.pagination import CursorSigner, encode_cursor
from pydantic import ValidationError
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

PROJECT_ROOT = Path(__file__).resolve().parents[3]
VERSIONS_DIR = PROJECT_ROOT / "apps" / "api" / "src" / "contentai" / "migrations" / "versions"

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


def test_release_version_and_expected_revision_chain():
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
        assert tomllib.load(stream)["project"]["version"] == "0.7.1"

    revisions = sorted(VERSIONS_DIR.glob("*.py"))
    assert [revision.name for revision in revisions] == [
        "202607210001_v050_initial_schema.py",
        "202608030001_model_pricing_and_usage_cost.py",
        "202608030002_restore_model_runtime_parameters.py",
        "202608040001_model_api_mode.py",
        "202608040002_model_usage_token_constraints.py",
        "202608040003_model_usage_cost_consistency.py",
        "202608040004_chatmessage_lineage_constraints.py",
        "202608040005_memory_numeric_constraints.py",
        "202608040006_current_attempt_lineage.py",
        "202608040007_agent_catalog_pagination_index.py",
        "202608040008_checkpoint_revision.py",
        "202608040009_event_stream_watermarks.py",
    ]
    script = ScriptDirectory.from_config(build_alembic_config())
    assert script.get_heads() == ["202608040009"]
    revision = script.get_revision("202607210001")
    assert revision is not None
    assert revision.down_revision is None
    pricing_revision = script.get_revision("202608030001")
    assert pricing_revision is not None
    assert pricing_revision.down_revision == "202607210001"
    runtime_revision = script.get_revision("202608030002")
    assert runtime_revision is not None
    assert runtime_revision.down_revision == "202608030001"
    api_mode_revision = script.get_revision("202608040001")
    assert api_mode_revision is not None
    assert api_mode_revision.down_revision == "202608030002"
    token_constraint_revision = script.get_revision("202608040002")
    assert token_constraint_revision is not None
    assert token_constraint_revision.down_revision == "202608040001"
    cost_consistency_revision = script.get_revision("202608040003")
    assert cost_consistency_revision is not None
    assert cost_consistency_revision.down_revision == "202608040002"
    chat_lineage_revision = script.get_revision("202608040004")
    assert chat_lineage_revision is not None
    assert chat_lineage_revision.down_revision == "202608040003"
    memory_numeric_revision = script.get_revision("202608040005")
    assert memory_numeric_revision is not None
    assert memory_numeric_revision.down_revision == "202608040004"
    current_attempt_revision = script.get_revision("202608040006")
    assert current_attempt_revision is not None
    assert current_attempt_revision.down_revision == "202608040005"
    catalog_index_revision = script.get_revision("202608040007")
    assert catalog_index_revision is not None
    assert catalog_index_revision.down_revision == "202608040006"
    checkpoint_revision = script.get_revision("202608040008")
    assert checkpoint_revision is not None
    assert checkpoint_revision.down_revision == "202608040007"
    stream_watermark_revision = script.get_revision("202608040009")
    assert stream_watermark_revision is not None
    assert stream_watermark_revision.down_revision == "202608040008"


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
    assert MessageListRequest(cursor=signed).cursor == signed
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


def test_agent_catalog_index_migration_roundtrip() -> None:
    config = build_alembic_config()
    engine = get_engine()
    index_name = "ix_agentprofile_user_created_id"

    try:
        command.downgrade(config, "202608040006")
        assert index_name not in {
            index["name"] for index in inspect(engine).get_indexes("agentprofile")
        }

        command.upgrade(config, "202608040007")
        indexes = {
            index["name"]: index for index in inspect(engine).get_indexes("agentprofile")
        }
        assert indexes[index_name]["column_names"] == ["user_id", "created_at", "id"]
        assert not indexes[index_name]["unique"]

        command.downgrade(config, "202608040006")
        assert index_name not in {
            index["name"] for index in inspect(engine).get_indexes("agentprofile")
        }
    finally:
        command.upgrade(config, "head")


def test_checkpoint_revision_migration_roundtrip() -> None:
    config = build_alembic_config()
    engine = get_engine()

    try:
        command.downgrade(config, "202608040007")
        assert "checkpoint_revision" not in _column_names(
            inspect(engine),
            "agentexecution",
        )
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO chatsession (
                        id, title, agent_id, agent_version_id,
                        langgraph_thread_id, user_id, created_at, updated_at
                    ) VALUES (
                        'checkpoint-revision-session', 'Checkpoint revision',
                        'default-agent', 'default-agent-v1',
                        'checkpoint-revision-thread', 'local-user', now(), now()
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO agentinvocation (
                        id, session_id, agent_id, user_id, created_at
                    ) VALUES (
                        'checkpoint-revision-invocation',
                        'checkpoint-revision-session',
                        'default-agent', 'local-user', now()
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO agentexecution (
                        id, invocation_id, session_id, agent_version_id,
                        model_config_id, trace_id, latest_checkpoint_id,
                        status, error, interrupt_payload, resume_payload,
                        attempt_count, next_attempt_kind, streaming_degraded,
                        streaming_degraded_reason, created_at, updated_at
                    ) VALUES (
                        'checkpoint-revision-execution',
                        'checkpoint-revision-invocation',
                        'checkpoint-revision-session', 'default-agent-v1',
                        'default-model-config', 'checkpoint-revision-trace', NULL,
                        'pending', '', '{}'::jsonb, '{}'::jsonb,
                        0, 'initial', false, '', now(), now()
                    )
                    """
                )
            )

        command.upgrade(config, "202608040008")
        columns = {
            column["name"]: column
            for column in inspect(engine).get_columns("agentexecution")
        }
        assert str(columns["checkpoint_revision"]["type"]).upper() == "BIGINT"
        assert columns["checkpoint_revision"]["nullable"] is False

        with engine.begin() as connection:
            revision = connection.execute(
                text(
                    "SELECT checkpoint_revision FROM agentexecution "
                    "WHERE id = 'checkpoint-revision-execution'"
                )
            ).scalar_one()
        assert revision == 0

        command.downgrade(config, "202608040007")
        assert "checkpoint_revision" not in _column_names(
            inspect(engine),
            "agentexecution",
        )
    finally:
        command.upgrade(config, "head")


def test_event_stream_watermark_migration_roundtrip_and_constraints() -> None:
    config = build_alembic_config()
    engine = get_engine()

    try:
        command.downgrade(config, "202608040008")
        assert "stream_committed_sequence" not in _column_names(
            inspect(engine),
            "agentexecution",
        )
        with engine.begin() as connection:
            for suffix in ("empty", "history"):
                connection.execute(
                    text(
                        """
                        INSERT INTO chatsession (
                            id, title, agent_id, agent_version_id,
                            langgraph_thread_id, user_id, created_at, updated_at
                        ) VALUES (
                            :session_id, 'Stream watermark migration',
                            'default-agent', 'default-agent-v1',
                            :thread_id, 'local-user', clock_timestamp(), clock_timestamp()
                        )
                        """
                    ),
                    {
                        "session_id": f"stream-watermark-session-{suffix}",
                        "thread_id": f"stream-watermark-thread-{suffix}",
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO agentinvocation (
                            id, session_id, agent_id, user_id, created_at
                        ) VALUES (
                            :invocation_id, :session_id,
                            'default-agent', 'local-user', clock_timestamp()
                        )
                        """
                    ),
                    {
                        "invocation_id": f"stream-watermark-invocation-{suffix}",
                        "session_id": f"stream-watermark-session-{suffix}",
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO agentexecution (
                            id, invocation_id, session_id, agent_version_id,
                            model_config_id, trace_id, status, error,
                            interrupt_payload, resume_payload, attempt_count,
                            next_attempt_kind, streaming_degraded,
                            streaming_degraded_reason, first_event_at,
                            created_at, updated_at
                        ) VALUES (
                            :execution_id, :invocation_id, :session_id,
                            'default-agent-v1', 'default-model-config', :trace_id,
                            'pending', '', '{}'::jsonb, '{}'::jsonb, 0,
                            'initial', false, '', :first_event_at,
                            clock_timestamp(), clock_timestamp()
                        )
                        """
                    ),
                    {
                        "execution_id": f"stream-watermark-execution-{suffix}",
                        "invocation_id": f"stream-watermark-invocation-{suffix}",
                        "session_id": f"stream-watermark-session-{suffix}",
                        "trace_id": f"stream-watermark-trace-{suffix}",
                        "first_event_at": (
                            None
                            if suffix == "empty"
                            else datetime(2026, 8, 4, tzinfo=UTC)
                        ),
                    },
                )

        command.upgrade(config, "202608040009")
        columns = {
            column["name"]: column
            for column in inspect(engine).get_columns("agentexecution")
        }
        assert str(columns["stream_committed_sequence"]["type"]).upper() == "BIGINT"
        assert columns["stream_committed_sequence"]["nullable"] is False
        assert columns["terminal_stream_sequence"]["nullable"] is True
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT id, stream_committed_sequence, streaming_degraded,
                           streaming_degraded_reason
                    FROM agentexecution
                    WHERE id LIKE 'stream-watermark-execution-%'
                    ORDER BY id
                    """
                )
            ).all()
        assert rows == [
            ("stream-watermark-execution-empty", 0, False, ""),
            (
                "stream-watermark-execution-history",
                0,
                True,
                "STREAM_WATERMARK_MIGRATION_REQUIRED",
            ),
        ]

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE agentexecution SET stream_committed_sequence = -1 "
                        "WHERE id = 'stream-watermark-execution-empty'"
                    )
                )
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE agentexecution
                        SET stream_committed_sequence = 2,
                            terminal_stream_sequence = 1,
                            terminal_stream_attempt_id = 'attempt-history',
                            terminal_stream_status = 'completed'
                        WHERE id = 'stream-watermark-execution-history'
                        """
                    )
                )
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE agentexecution
                    SET stream_committed_sequence = 2,
                        terminal_stream_sequence = 2,
                        terminal_stream_attempt_id = 'attempt-history',
                        terminal_stream_status = 'completed'
                    WHERE id = 'stream-watermark-execution-history'
                    """
                )
            )

        command.downgrade(config, "202608040008")
        downgraded_columns = _column_names(inspect(engine), "agentexecution")
        assert "stream_committed_sequence" not in downgraded_columns
        assert "terminal_stream_sequence" not in downgraded_columns
        assert "terminal_stream_attempt_id" not in downgraded_columns
        assert "terminal_stream_status" not in downgraded_columns
    finally:
        command.upgrade(config, "head")


def _render_offline_upgrade(revision_range: str) -> str:
    config = build_alembic_config()
    output = StringIO()
    config.output_buffer = output
    command.upgrade(config, revision_range, sql=True)
    return output.getvalue()


def test_offline_baseline_noop_does_not_emit_legacy_usage_repair() -> None:
    output = _render_offline_upgrade("202607210001:202607210001")

    assert "LOCK TABLE modelusage IN SHARE ROW EXCLUSIVE MODE" not in output
    assert "SET input_tokens = GREATEST(input_tokens, 0)" not in output


def test_offline_baseline_upgrade_emits_repair_before_pricing_revision() -> None:
    output = _render_offline_upgrade("202607210001:202608030001")
    lock_position = output.index(
        "LOCK TABLE modelusage IN SHARE ROW EXCLUSIVE MODE"
    )
    repair_position = output.index("SET input_tokens = GREATEST(input_tokens, 0)")
    pricing_position = output.index(
        "-- Running upgrade 202607210001 -> 202608030001"
    )

    assert output.count("LOCK TABLE modelusage IN SHARE ROW EXCLUSIVE MODE") == 1
    assert output.count("SET input_tokens = GREATEST(input_tokens, 0)") == 1
    assert lock_position < repair_position < pricing_position


def test_offline_base_upgrade_does_not_repair_before_schema_creation() -> None:
    output = _render_offline_upgrade("base:202608030001")

    assert "LOCK TABLE modelusage IN SHARE ROW EXCLUSIVE MODE" not in output
    assert output.index("CREATE TABLE modelusage") < output.index(
        "-- Running upgrade 202607210001 -> 202608030001"
    )


def test_baseline_noop_upgrade_preserves_legacy_usage_tokens() -> None:
    config = build_alembic_config()
    engine = get_engine()

    command.downgrade(config, "202607210001")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO modelusage (
                        id, call_id, user_id, session_id, execution_id,
                        category, provider, latency_ms, status, model_name,
                        input_tokens, output_tokens, total_tokens,
                        usage_available, created_at
                    )
                    VALUES (
                        'usage-baseline-noop', 'baseline-noop-call',
                        'local-user', NULL, NULL, 'migration-test', 'test',
                        NULL, 'completed', 'test-model', -7, -3, -10, true, now()
                    )
                    """
                )
            )

        command.upgrade(config, "202607210001")

        with engine.connect() as connection:
            token_values = connection.execute(
                text(
                    """
                    SELECT input_tokens, output_tokens, total_tokens
                    FROM modelusage
                    WHERE id = 'usage-baseline-noop'
                    """
                )
            ).one()
        assert token_values == (-7, -3, -10)
    finally:
        command.upgrade(config, "head")


def test_model_api_mode_migration_backfills_and_constrains_existing_rows() -> None:
    config = build_alembic_config()
    engine = get_engine()

    try:
        command.downgrade(config, "202608030002")
        assert "api_mode" not in _column_names(inspect(engine), "modelconfiguration")

        command.upgrade(config, "202608040001")
        model_columns = {
            column["name"]: column
            for column in inspect(engine).get_columns("modelconfiguration")
        }
        api_mode_column = model_columns["api_mode"]
        assert not api_mode_column["nullable"]
        assert "chat_completions" in str(api_mode_column["default"])

        constraint_names = {
            constraint["name"]
            for constraint in inspect(engine).get_check_constraints("modelconfiguration")
        }
        assert "ck_modelconfiguration_api_mode" in constraint_names

        with engine.connect() as connection:
            backfilled_mode = connection.execute(
                text(
                    "SELECT api_mode FROM modelconfiguration "
                    "WHERE id = 'default-model-config'"
                )
            ).scalar_one()
        assert backfilled_mode == "chat_completions"

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE modelconfiguration SET api_mode = 'auto' "
                        "WHERE id = 'default-model-config'"
                    )
                )
    finally:
        command.upgrade(config, "head")


def test_usage_migrations_normalize_and_constrain_historical_tokens() -> None:
    config = build_alembic_config()
    engine = get_engine()

    command.downgrade(config, "202607210001")
    try:
        # The published 202608030001 migration predates token constraints and
        # remains immutable. The Alembic online environment repairs a manually
        # corrupted baseline before that revision computes costs, then 040002
        # remains the in-schema repair for rows written after 030001.
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO chatsession (
                        id, title, agent_id, agent_version_id,
                        langgraph_thread_id, user_id, created_at, updated_at
                    )
                    VALUES (
                        'dirty-baseline-session', 'Dirty baseline', 'default-agent',
                        'default-agent-v1', 'dirty-baseline-thread', 'local-user',
                        now(), now()
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO agentinvocation (
                        id, session_id, agent_id, user_id, created_at
                    )
                    VALUES (
                        'dirty-baseline-invocation', 'dirty-baseline-session',
                        'default-agent', 'local-user', now()
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO agentexecution (
                        id, invocation_id, session_id, agent_version_id,
                        model_config_id, trace_id, latest_checkpoint_id,
                        status, error, interrupt_payload, resume_payload,
                        started_at, finished_at, cancel_requested_at,
                        claimed_at, heartbeat_at, lease_expires_at, worker_id,
                        attempt_count, next_attempt_kind, current_attempt_id,
                        first_event_at, first_token_at, streaming_degraded,
                        streaming_degraded_at, streaming_degraded_reason,
                        postprocess_completed_at, created_at, updated_at
                    )
                    VALUES (
                        'dirty-baseline-execution', 'dirty-baseline-invocation',
                        'dirty-baseline-session', 'default-agent-v1',
                        'default-model-config', 'dirty-baseline-trace', NULL,
                        'completed', '', '{}'::jsonb, '{}'::jsonb,
                        now(), now(), NULL, NULL, NULL, NULL, NULL,
                        1, 'initial', NULL, now(), now(), false, NULL, '',
                        now(), now(), now()
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO modelusage (
                        id, call_id, user_id, session_id, execution_id,
                        category, provider, latency_ms, status, model_name,
                        input_tokens, output_tokens, total_tokens,
                        usage_available, created_at
                    )
                    VALUES (
                        'usage-dirty-baseline', 'dirty-baseline-call',
                        'local-user', 'dirty-baseline-session',
                        'dirty-baseline-execution', 'migration-test', 'test',
                        NULL, 'completed', 'test-model', -7, -3, -10, true, now()
                    )
                    """
                )
            )

        command.upgrade(config, "202608030001")
        with engine.connect() as connection:
            repaired_baseline = connection.execute(
                text(
                    """
                    SELECT input_tokens, output_tokens, total_tokens,
                           input_cost_usd, output_cost_usd, total_cost_usd
                    FROM modelusage
                    WHERE id = 'usage-dirty-baseline'
                    """
                )
            ).one()
        assert repaired_baseline == (
            0,
            0,
            0,
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
        )

        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO chatsession (
                        id, title, agent_id, agent_version_id,
                        langgraph_thread_id, user_id, created_at, updated_at
                    )
                    VALUES (
                        'migration-session', 'Migration test', 'default-agent',
                        'default-agent-v1', 'migration-thread', 'local-user',
                        now(), now()
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO agentinvocation (
                        id, session_id, agent_id, user_id, created_at
                    )
                    VALUES (
                        'migration-invocation', 'migration-session',
                        'default-agent', 'local-user', now()
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO agentexecution (
                        id, invocation_id, session_id, agent_version_id,
                        model_config_id, trace_id, latest_checkpoint_id,
                        status, error, interrupt_payload, resume_payload,
                        started_at, finished_at, cancel_requested_at,
                        claimed_at, heartbeat_at, lease_expires_at, worker_id,
                        attempt_count, next_attempt_kind, current_attempt_id,
                        first_event_at, first_token_at, streaming_degraded,
                        streaming_degraded_at, streaming_degraded_reason,
                        postprocess_completed_at, created_at, updated_at
                    )
                    VALUES (
                        'migration-execution', 'migration-invocation',
                        'migration-session', 'default-agent-v1',
                        'default-model-config', 'migration-trace', NULL,
                        'completed', '', '{}'::jsonb, '{}'::jsonb,
                        now(), now(), NULL, NULL, NULL, NULL, NULL,
                        1, 'initial', NULL, now(), now(), false, NULL, '',
                        now(), now(), now()
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO modelusage (
                        id, call_id, user_id, session_id, execution_id,
                        category, provider, latency_ms, status, model_name,
                        input_tokens, output_tokens, total_tokens,
                        usage_available, created_at
                    )
                    VALUES
                        (
                            'usage-negative-migration', 'negative-migration-call',
                            'local-user', 'migration-session', 'migration-execution',
                            'migration-test', 'test',
                            NULL, 'completed', 'test-model', -7, -3, -10, true, now()
                        ),
                        (
                            'usage-inconsistent-migration', 'inconsistent-migration-call',
                            'local-user', 'migration-session', 'migration-execution',
                            'migration-test', 'test',
                            NULL, 'completed', 'test-model', 4, 5, 2, true, now()
                        ),
                        (
                            'usage-short-total-migration', 'short-total-migration-call',
                            'local-user', 'migration-session', 'migration-execution',
                            'migration-test', 'test',
                            NULL, 'completed', 'test-model', 10, 5, 12, true, now()
                        )
                    """
                )
            )

        with engine.connect() as connection:
            historical_token_values = connection.execute(
                text(
                    """
                    SELECT id, input_tokens, output_tokens, total_tokens
                    FROM modelusage
                    WHERE id IN (
                        'usage-negative-migration',
                        'usage-inconsistent-migration',
                        'usage-short-total-migration'
                    )
                    ORDER BY id
                    """
                )
            ).all()

        pricing_constraint_names = {
            constraint["name"]
            for constraint in inspect(engine).get_check_constraints("modelusage")
        }
        assert historical_token_values == [
            ("usage-inconsistent-migration", 4, 5, 2),
            ("usage-negative-migration", -7, -3, -10),
            ("usage-short-total-migration", 10, 5, 12),
        ]
        assert {
            "ck_modelusage_input_cost_nonnegative",
            "ck_modelusage_output_cost_nonnegative",
            "ck_modelusage_total_cost_nonnegative",
        } <= pricing_constraint_names

        command.upgrade(config, "202608040002")
        with engine.connect() as connection:
            normalized_token_values = connection.execute(
                text(
                    """
                    SELECT id, input_tokens, output_tokens, total_tokens
                    FROM modelusage
                    WHERE id IN (
                        'usage-negative-migration',
                        'usage-inconsistent-migration',
                        'usage-short-total-migration'
                    )
                    ORDER BY id
                    """
                )
            ).all()

        assert normalized_token_values == [
            ("usage-inconsistent-migration", 4, 5, 9),
            ("usage-negative-migration", 0, 0, 0),
            ("usage-short-total-migration", 10, 5, 15),
        ]
        token_constraint_names = {
            constraint["name"]
            for constraint in inspect(engine).get_check_constraints("modelusage")
        }
        assert {
            "ck_modelusage_input_tokens_nonnegative",
            "ck_modelusage_output_tokens_nonnegative",
            "ck_modelusage_total_tokens_nonnegative",
            "ck_modelusage_total_tokens_cover_parts",
        } <= token_constraint_names
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE modelusage
                        SET input_tokens = -1
                        WHERE id = 'usage-short-total-migration'
                        """
                    )
                )
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE modelusage
                        SET total_tokens = 14
                        WHERE id = 'usage-short-total-migration'
                        """
                    )
                )

        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE modelusage
                    SET input_cost_usd = 0.0000000001,
                        output_cost_usd = 0.0000000001,
                        total_cost_usd = 0.0000000001
                    WHERE id = 'usage-short-total-migration'
                    """
                )
            )

        command.upgrade(config, "202608040003")
        with engine.connect() as connection:
            repaired_total_cost = connection.execute(
                text(
                    """
                    SELECT total_cost_usd
                    FROM modelusage
                    WHERE id = 'usage-short-total-migration'
                    """
                )
            ).scalar_one()
        assert repaired_total_cost == Decimal("0.0000000002")

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        UPDATE modelusage
                        SET total_cost_usd = 0
                        WHERE id = 'usage-short-total-migration'
                        """
                    )
                )
    finally:
        command.upgrade(config, "head")


def test_tenant_and_execution_lineage_constraints_are_database_enforced():
    inspector = inspect(get_engine())
    assert {"must_change_password", "temporary_password_expires_at"} <= _column_names(
        inspector, "appuser"
    )
    assert "session_id" in _column_names(inspector, "agentexecution")
    assert "execution_id" in _column_names(inspector, "chatmessage")
    assert "payload" in _column_names(inspector, "executionoutbox")
    assert "model_config_id" in _column_names(inspector, "agentexecution")
    assert "checkpoint_revision" in _column_names(inspector, "agentexecution")
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
