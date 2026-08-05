"""enforce nonnegative model usage token counts

Revision ID: 202608040002
Revises: 202608040001
Create Date: 2026-08-04 12:15:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202608040002"
down_revision: Union[str, Sequence[str], None] = "202608040001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "modelusage"
TOKEN_CONSTRAINTS = {
    "ck_modelusage_input_tokens_nonnegative": "input_tokens >= 0",
    "ck_modelusage_output_tokens_nonnegative": "output_tokens >= 0",
    "ck_modelusage_total_tokens_nonnegative": "total_tokens >= 0",
    "ck_modelusage_total_tokens_cover_parts": (
        "total_tokens >= input_tokens + output_tokens"
    ),
}


def _check_constraint_names() -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(TABLE_NAME)
        if constraint.get("name")
    }


def upgrade() -> None:
    # Some databases may already contain rows written before the application
    # clamped provider token metadata. Repair those rows before adding the
    # invariant so an in-place production upgrade cannot fail halfway through.
    op.execute(
        """
        UPDATE modelusage
        SET input_tokens = GREATEST(input_tokens, 0),
            output_tokens = GREATEST(output_tokens, 0),
            total_tokens = GREATEST(
                total_tokens,
                GREATEST(input_tokens, 0) + GREATEST(output_tokens, 0),
                0
            )
        WHERE input_tokens < 0
           OR output_tokens < 0
           OR total_tokens < 0
           OR total_tokens < GREATEST(input_tokens, 0) + GREATEST(output_tokens, 0)
        """
    )

    existing_constraints = _check_constraint_names()
    for constraint_name, condition in TOKEN_CONSTRAINTS.items():
        if constraint_name not in existing_constraints:
            op.create_check_constraint(constraint_name, TABLE_NAME, condition)


def downgrade() -> None:
    existing_constraints = _check_constraint_names()
    for constraint_name in reversed(TOKEN_CONSTRAINTS):
        if constraint_name in existing_constraints:
            op.drop_constraint(constraint_name, TABLE_NAME, type_="check")
