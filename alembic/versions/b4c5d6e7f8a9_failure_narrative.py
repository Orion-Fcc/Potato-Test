"""run_result.failure_narrative: AI-written bug description for failed cases

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-10-02 12:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b4c5d6e7f8a9"
down_revision: Union[str, None] = "a3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # JSON: {steps, actual, expected, title, severity}. NULL for passed cases, and NULL
    # when generation failed — so the report just shows nothing rather than an empty shell.
    with op.batch_alter_table("run_result", schema=None) as b:
        b.add_column(sa.Column("failure_narrative", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("run_result", schema=None) as b:
        b.drop_column("failure_narrative")
