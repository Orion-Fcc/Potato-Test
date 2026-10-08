"""run_result.attribution: who caused the failure

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-10-07 16:10:00.000000

Why this column exists
======================
``root_cause`` (added 2026-10-04) answers "why did the case fail". This one answers a
different question: "whose fault was it". Two measured failures on2026-10-07 looked
like ordinary ``root_cause`` failures and were not defects at all:

* a virtualised list read mid-request showed 「共 0 条」, so the agent concluded the
  trainee it had just created did not exist;
* an import that was never submitted for approval looked like data loss.

Both would have been filed as defects for a developer to investigate, and both would
have consumed triage time on code that works. Neither was visible from ``root_cause``,
because the judge reads only what the agent reported — and the agent's report was the
thing that was wrong. The signal that separates them lives in the step evidence, which
is what :mod:`app.failure_attrib` reads.

Nullable, like ``root_cause``: NULL means "not computed" (older rows, or a run with
narratives disabled) and is deliberately distinct from an empty string, which means
"computed and inconclusive" (``unknown``).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c5d6e7f8a9b0"
down_revision: Union[str, None] = "b4c5d6e7f8a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # One of: system | setup | transient | agent | unknown (see app/failure_attrib.py).
    with op.batch_alter_table("run_result", schema=None) as b:
        b.add_column(sa.Column("attribution", sa.String(30), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("run_result", schema=None) as b:
        b.drop_column("attribution")