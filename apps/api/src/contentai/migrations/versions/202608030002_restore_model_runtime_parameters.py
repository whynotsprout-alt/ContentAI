"""restore versioned model runtime parameters

Revision ID: 202608030002
Revises: 202608030001
Create Date: 2026-08-03 18:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202608030002"
down_revision: Union[str, Sequence[str], None] = "202608030001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE_NAME = "modelconfiguration"

RUNTIME_COLUMNS = {
    "temperature": sa.Column(
        "temperature",
        sa.Float(),
        server_default=sa.text("0.2"),
        nullable=True,
    ),
    "context_window_tokens": sa.Column(
        "context_window_tokens",
        sa.Integer(),
        server_default=sa.text("32000"),
        nullable=False,
    ),
    "chat_max_tokens": sa.Column(
        "chat_max_tokens",
        sa.Integer(),
        server_default=sa.text("8000"),
        nullable=False,
    ),
    "structured_max_tokens": sa.Column(
        "structured_max_tokens",
        sa.Integer(),
        server_default=sa.text("8000"),
        nullable=False,
    ),
}

RUNTIME_CONSTRAINTS = {
    "ck_modelconfiguration_temperature_range": (
        "temperature IS NULL OR (temperature >= 0 AND temperature <= 2)"
    ),
    "ck_modelconfiguration_context_window_positive": "context_window_tokens > 0",
    "ck_modelconfiguration_chat_output_fits_context": (
        "chat_max_tokens > 0 AND chat_max_tokens < context_window_tokens"
    ),
    "ck_modelconfiguration_structured_output_fits_context": (
        "structured_max_tokens > 0 AND structured_max_tokens < context_window_tokens"
    ),
}


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
    existing_columns = _column_names()
    for column_name, column in RUNTIME_COLUMNS.items():
        if column_name not in existing_columns:
            op.add_column(TABLE_NAME, column)

    existing_constraints = _check_constraint_names()
    for constraint_name, condition in RUNTIME_CONSTRAINTS.items():
        if constraint_name not in existing_constraints:
            op.create_check_constraint(
                constraint_name,
                TABLE_NAME,
                condition,
            )


def downgrade() -> None:
    existing_constraints = _check_constraint_names()
    for constraint_name in reversed(RUNTIME_CONSTRAINTS):
        if constraint_name in existing_constraints:
            op.drop_constraint(constraint_name, TABLE_NAME, type_="check")

    existing_columns = _column_names()
    for column_name in reversed(RUNTIME_COLUMNS):
        if column_name in existing_columns:
            op.drop_column(TABLE_NAME, column_name)
