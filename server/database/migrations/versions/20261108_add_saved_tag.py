"""add saved-article tag

Revision ID: 20261108tag
Revises: 20261106indexes

The collection page distinguishes "liked" from "read it later" without a
second table. The column is nullable so existing rows and older backups stay
valid. The check is idempotent because ``Base.metadata.create_all`` in tests
already produces the newest schema.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261108tag"
down_revision: str | Sequence[str] | None = "20261106indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("reading_states")}
    if "tag" not in columns:
        op.add_column(
            "reading_states", sa.Column("tag", sa.String(length=16), nullable=True)
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("reading_states")}
    if "tag" in columns:
        op.drop_column("reading_states", "tag")
