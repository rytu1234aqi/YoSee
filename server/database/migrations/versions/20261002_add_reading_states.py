"""add reading states

Revision ID: 20261002reading
Revises: 402160967718
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261002reading"
down_revision: str | Sequence[str] | None = "402160967718"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reading_states",
        sa.Column("content_id", sa.String(length=36), nullable=False),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("saved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("saved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["content_id"], ["content_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("content_id", name=op.f("pk_reading_states")),
    )
    op.create_index("ix_reading_states_saved", "reading_states", ["saved"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_reading_states_saved", table_name="reading_states")
    op.drop_table("reading_states")
