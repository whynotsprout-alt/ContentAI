"""keep model usage total cost equal to its component costs

Revision ID: 202608040003
Revises: 202608040002
Create Date: 2026-08-04 12:30:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202608040003"
down_revision: Union[str, Sequence[str], None] = "202608040002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "modelusage"
CONSTRAINT_NAME = "ck_modelusage_total_cost_matches_parts"


def _check_constraint_names() -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(TABLE_NAME)
        if constraint.get("name")
    }


def upgrade() -> None:
    # Older pricing backfills rounded the combined raw cost, while runtime
    # accounting rounds each component before summing. Normalize persisted
    # totals so historical and newly recorded calls use identical arithmetic.
    op.execute(
        """
        UPDATE modelusage
        SET total_cost_usd = input_cost_usd + output_cost_usd
        WHERE total_cost_usd IS DISTINCT FROM input_cost_usd + output_cost_usd
        """
    )
    if CONSTRAINT_NAME not in _check_constraint_names():
        op.create_check_constraint(
            CONSTRAINT_NAME,
            TABLE_NAME,
            "total_cost_usd = input_cost_usd + output_cost_usd",
        )


def downgrade() -> None:
    if CONSTRAINT_NAME in _check_constraint_names():
        op.drop_constraint(CONSTRAINT_NAME, TABLE_NAME, type_="check")
