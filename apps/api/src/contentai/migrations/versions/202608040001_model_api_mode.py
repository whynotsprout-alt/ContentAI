"""store the explicit OpenAI API mode for each model configuration

Revision ID: 202608040001
Revises: 202608030002
Create Date: 2026-08-04 12:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202608040001"
down_revision: Union[str, Sequence[str], None] = "202608030002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "modelconfiguration"
COLUMN_NAME = "api_mode"
CONSTRAINT_NAME = "ck_modelconfiguration_api_mode"
DEFAULT_API_MODE = "chat_completions"
API_MODE_CHECK = "api_mode IN ('chat_completions', 'responses')"


def _column_names() -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(TABLE_NAME)
    }


def _check_constraint_names() -> set[str]:
    return {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(TABLE_NAME)
        if constraint.get("name")
    }


def upgrade() -> None:
    if COLUMN_NAME not in _column_names():
        op.add_column(
            TABLE_NAME,
            sa.Column(
                COLUMN_NAME,
                sa.String(length=24),
                server_default=sa.text(f"'{DEFAULT_API_MODE}'"),
                nullable=False,
            ),
        )
    if CONSTRAINT_NAME not in _check_constraint_names():
        op.create_check_constraint(CONSTRAINT_NAME, TABLE_NAME, API_MODE_CHECK)


def downgrade() -> None:
    if CONSTRAINT_NAME in _check_constraint_names():
        op.drop_constraint(CONSTRAINT_NAME, TABLE_NAME, type_="check")
    if COLUMN_NAME in _column_names():
        op.drop_column(TABLE_NAME, COLUMN_NAME)
