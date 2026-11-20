"""YouReel: client error reports table (errors a member hit in the studio's browser UI)

Revision ID: 50a4847b9836
Revises: b5d1e8f3a627
Create Date: 2026-11-20 10:00:00.000000

The studio page reports error toasts, uncaught exceptions and failed requests; they are kept here (newest 1000 rows,
pruned on insert) and listed in Settings → Options → Diagnostics. A new table, nothing to backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "50a4847b9836"
down_revision: str | Sequence[str] | None = "b5d1e8f3a627"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_error_reports",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("app_build", sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    op.drop_table("client_error_reports")
