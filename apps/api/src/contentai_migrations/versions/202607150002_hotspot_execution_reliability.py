"""Add postprocess attempts and remove the Bloomberg hotspot source.

Revision ID: 202607150002
Revises: 202607150001
Create Date: 2026-07-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "202607150002"
down_revision: str | None = "202607150001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_HOTSPOT_SOURCES = """[
  "36kr", "cls", "eeo", "yicai", "huxiu", "jiemian", "tmtpost", "latepost",
  "qbitai", "leiphone", "caixin", "vista", "ft", "wsj", "techcrunch",
  "theverge", "ifanr", "stcn", "douyin", "bilibili", "xiaohongshu",
  "weibo", "aihot"
]"""


def upgrade() -> None:
    op.add_column(
        "executionoutbox",
        sa.Column(
            "processing_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.alter_column("executionoutbox", "processing_attempts", server_default=None)
    op.execute(
        sa.text(
            f"""
            WITH cleaned AS (
                SELECT
                    version.id,
                    COALESCE(
                        jsonb_agg(source.value) FILTER (
                            WHERE source.value <> '"bloomberg"'::jsonb
                        ),
                        '[]'::jsonb
                    ) AS sources
                FROM agentversion AS version
                CROSS JOIN LATERAL jsonb_array_elements(
                    CASE
                        WHEN jsonb_typeof(version.tools_config->'hotspot_sources') = 'array'
                        THEN version.tools_config->'hotspot_sources'
                        ELSE '[]'::jsonb
                    END
                ) AS source(value)
                WHERE version.tools_config->'hotspot_sources' @> '["bloomberg"]'::jsonb
                GROUP BY version.id
            )
            UPDATE agentversion AS version
            SET tools_config = jsonb_set(
                version.tools_config,
                '{{hotspot_sources}}',
                CASE
                    WHEN jsonb_array_length(cleaned.sources) = 0
                    THEN '{DEFAULT_HOTSPOT_SOURCES}'::jsonb
                    ELSE cleaned.sources
                END,
                true
            )
            FROM cleaned
            WHERE version.id = cleaned.id
            """
        )
    )


def downgrade() -> None:
    op.drop_column("executionoutbox", "processing_attempts")
