import pytest
from alembic import command
from contentai.core.alembic import build_alembic_config
from contentai.db.session import get_engine
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

CONSTRAINT_NAMES = {
    "ck_memoryrecord_confidence_unit_interval",
    "ck_memoryrecord_importance_unit_interval",
    "ck_memoryrecord_version_positive",
    "ck_memoryrecord_access_count_nonnegative",
}


def _memory_constraint_names() -> set[str]:
    return {
        constraint["name"]
        for constraint in inspect(get_engine()).get_check_constraints("memoryrecord")
        if constraint.get("name")
    }


def test_memory_numeric_migration_repairs_history_and_enforces_constraints() -> None:
    config = build_alembic_config()
    engine = get_engine()

    command.downgrade(config, "202608040004")
    try:
        assert CONSTRAINT_NAMES.isdisjoint(_memory_constraint_names())
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO memoryrecord (
                        id, user_id, agent_id, session_id, memory_key, kind,
                        payload, content, confidence, importance_score,
                        source_type, version, access_count, created_at, updated_at
                    )
                    VALUES
                        (
                            'memory-nan', 'local-user', 'default-agent', NULL,
                            'migration-nan', 'semantic', '{}'::jsonb, 'NaN values',
                            'NaN'::double precision, '-Infinity'::double precision,
                            'manual', 0, -3, now(), now()
                        ),
                        (
                            'memory-positive-inf', 'local-user', 'default-agent', NULL,
                            'migration-positive-inf', 'semantic', '{}'::jsonb,
                            'positive infinity values',
                            'Infinity'::double precision, 'Infinity'::double precision,
                            'manual', -4, -2, now(), now()
                        ),
                        (
                            'memory-negative-inf', 'local-user', 'default-agent', NULL,
                            'migration-negative-inf', 'semantic', '{}'::jsonb,
                            'negative infinity values',
                            '-Infinity'::double precision, 'NaN'::double precision,
                            'manual', 1, 0, now(), now()
                        ),
                        (
                            'memory-range', 'local-user', 'default-agent', NULL,
                            'migration-range', 'semantic', '{}'::jsonb,
                            'out of range values', -0.25, 1.5,
                            'manual', 2, 3, now(), now()
                        )
                    """
                )
            )

        command.upgrade(config, "202608040005")

        with engine.connect() as connection:
            repaired = connection.execute(
                text(
                    """
                    SELECT id, confidence, importance_score, version, access_count
                    FROM memoryrecord
                    WHERE id LIKE 'memory-%'
                    ORDER BY id
                    """
                )
            ).all()

        assert repaired == [
            ("memory-nan", 0.0, 0.0, 1, 0),
            ("memory-negative-inf", 0.0, 0.0, 1, 0),
            ("memory-positive-inf", 1.0, 1.0, 1, 0),
            ("memory-range", 0.0, 1.0, 2, 3),
        ]
        assert CONSTRAINT_NAMES <= _memory_constraint_names()

        invalid_assignments = (
            "confidence = 'NaN'::double precision",
            "confidence = 'Infinity'::double precision",
            "importance_score = '-Infinity'::double precision",
            "importance_score = 1.01",
            "version = 0",
            "access_count = -1",
        )
        for assignment in invalid_assignments:
            with pytest.raises(IntegrityError):
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            f"UPDATE memoryrecord SET {assignment} "
                            "WHERE id = 'memory-range'"
                        )
                    )

        command.downgrade(config, "202608040004")
        assert CONSTRAINT_NAMES.isdisjoint(_memory_constraint_names())
        with engine.connect() as connection:
            still_repaired = connection.execute(
                text(
                    """
                    SELECT confidence, importance_score, version, access_count
                    FROM memoryrecord
                    WHERE id = 'memory-range'
                    """
                )
            ).one()
        assert still_repaired == (0.0, 1.0, 2, 3)
    finally:
        command.upgrade(config, "head")
