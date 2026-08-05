"""constrain memory scores and counters

Revision ID: 202608040005
Revises: 202608040004
Create Date: 2026-08-04 15:30:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202608040005"
down_revision: Union[str, Sequence[str], None] = "202608040004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "memoryrecord"
CHECK_CONSTRAINTS = {
    "ck_memoryrecord_confidence_unit_interval": (
        "confidence NOT IN ("
        "'NaN'::double precision, "
        "'Infinity'::double precision, "
        "'-Infinity'::double precision"
        ") AND confidence BETWEEN 0 AND 1"
    ),
    "ck_memoryrecord_importance_unit_interval": (
        "importance_score NOT IN ("
        "'NaN'::double precision, "
        "'Infinity'::double precision, "
        "'-Infinity'::double precision"
        ") AND importance_score BETWEEN 0 AND 1"
    ),
    "ck_memoryrecord_version_positive": "version > 0",
    "ck_memoryrecord_access_count_nonnegative": "access_count >= 0",
}


def _check_constraint_names() -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(TABLE_NAME)
        if constraint.get("name")
    }


def upgrade() -> None:
    # PostgreSQL deliberately sorts NaN above every finite float, so a plain
    # lower/upper clamp is not sufficient. Handle it before the range cases,
    # then deterministically repair counters that predate these constraints.
    op.execute(
        """
        UPDATE memoryrecord
        SET confidence = CASE
                WHEN confidence = 'NaN'::double precision THEN 0
                WHEN confidence < 0 THEN 0
                WHEN confidence > 1 THEN 1
                ELSE confidence
            END,
            importance_score = CASE
                WHEN importance_score = 'NaN'::double precision THEN 0
                WHEN importance_score < 0 THEN 0
                WHEN importance_score > 1 THEN 1
                ELSE importance_score
            END,
            version = CASE WHEN version > 0 THEN version ELSE 1 END,
            access_count = CASE WHEN access_count >= 0 THEN access_count ELSE 0 END
        WHERE confidence IN (
                'NaN'::double precision,
                'Infinity'::double precision,
                '-Infinity'::double precision
            )
           OR confidence < 0
           OR confidence > 1
           OR importance_score IN (
                'NaN'::double precision,
                'Infinity'::double precision,
                '-Infinity'::double precision
            )
           OR importance_score < 0
           OR importance_score > 1
           OR version <= 0
           OR access_count < 0
        """
    )
    existing = _check_constraint_names()
    for name, condition in CHECK_CONSTRAINTS.items():
        if name not in existing:
            op.create_check_constraint(name, TABLE_NAME, condition)


def downgrade() -> None:
    existing = _check_constraint_names()
    for name in reversed(CHECK_CONSTRAINTS):
        if name in existing:
            op.drop_constraint(name, TABLE_NAME, type_="check")
