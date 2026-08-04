"""add model pricing and usage cost snapshots

Revision ID: 202608030001
Revises: 202607210001
Create Date: 2026-08-03 14:00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "202608030001"
down_revision: Union[str, Sequence[str], None] = "202607210001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "modelconfiguration",
        sa.Column(
            "input_price_per_million_usd",
            sa.Numeric(precision=12, scale=6),
            server_default=sa.text("5.000000"),
            nullable=False,
        ),
    )
    op.add_column(
        "modelconfiguration",
        sa.Column(
            "output_price_per_million_usd",
            sa.Numeric(precision=12, scale=6),
            server_default=sa.text("25.000000"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_modelconfiguration_input_price_nonnegative",
        "modelconfiguration",
        "input_price_per_million_usd >= 0",
    )
    op.create_check_constraint(
        "ck_modelconfiguration_output_price_nonnegative",
        "modelconfiguration",
        "output_price_per_million_usd >= 0",
    )

    for column_name in ("input_cost_usd", "output_cost_usd", "total_cost_usd"):
        op.add_column(
            "modelusage",
            sa.Column(
                column_name,
                sa.Numeric(precision=20, scale=10),
                server_default=sa.text("0"),
                nullable=False,
            ),
        )

    # Existing calls are priced against the immutable configuration captured
    # by their execution, using the new Claude Opus 4.8 defaults.
    op.execute(
        """
        UPDATE modelusage AS usage
        SET input_cost_usd = ROUND(
                usage.input_tokens * config.input_price_per_million_usd / 1000000,
                10
            ),
            output_cost_usd = ROUND(
                usage.output_tokens * config.output_price_per_million_usd / 1000000,
                10
            ),
            total_cost_usd = ROUND(
                (
                    usage.input_tokens * config.input_price_per_million_usd
                    + usage.output_tokens * config.output_price_per_million_usd
                ) / 1000000,
                10
            )
        FROM agentexecution AS execution
        JOIN modelconfiguration AS config
          ON config.id = execution.model_config_id
        WHERE usage.execution_id = execution.id
          AND usage.usage_available IS TRUE
        """
    )
    op.create_check_constraint(
        "ck_modelusage_input_cost_nonnegative",
        "modelusage",
        "input_cost_usd >= 0",
    )
    op.create_check_constraint(
        "ck_modelusage_output_cost_nonnegative",
        "modelusage",
        "output_cost_usd >= 0",
    )
    op.create_check_constraint(
        "ck_modelusage_total_cost_nonnegative",
        "modelusage",
        "total_cost_usd >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_modelusage_total_cost_nonnegative", "modelusage", type_="check")
    op.drop_constraint("ck_modelusage_output_cost_nonnegative", "modelusage", type_="check")
    op.drop_constraint("ck_modelusage_input_cost_nonnegative", "modelusage", type_="check")
    op.drop_column("modelusage", "total_cost_usd")
    op.drop_column("modelusage", "output_cost_usd")
    op.drop_column("modelusage", "input_cost_usd")
    op.drop_constraint(
        "ck_modelconfiguration_output_price_nonnegative",
        "modelconfiguration",
        type_="check",
    )
    op.drop_constraint(
        "ck_modelconfiguration_input_price_nonnegative",
        "modelconfiguration",
        type_="check",
    )
    op.drop_column("modelconfiguration", "output_price_per_million_usd")
    op.drop_column("modelconfiguration", "input_price_per_million_usd")
