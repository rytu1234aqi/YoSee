"""add composite feed indexes

Revision ID: 20261106indexes
Revises: 20261002reading

Feed queries sort by ``(published_at, id)`` and filter by ``source_id`` and
``reading_states.is_read``. Single-column indexes cannot serve that shape.
Foreign-key cascades are not rebuilt here: SQLite cannot alter a constraint
in place, and ``delete_source`` removes children in the same transaction, so
existing databases remain correct without a table recreation.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261106indexes"
down_revision: str | Sequence[str] | None = "20261002reading"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ix_content_items_published_id", "content_items", ("published_at", "id")),
    (
        "ix_content_items_source_published_id",
        "content_items",
        ("source_id", "published_at", "id"),
    ),
    ("ix_reading_states_is_read", "reading_states", ("is_read",)),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for name, table, columns in _INDEXES:
        existing = {index["name"] for index in inspector.get_indexes(table)}
        if name not in existing:
            op.create_index(name, table, list(columns), unique=False)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for name, table, _ in _INDEXES:
        existing = {index["name"] for index in inspector.get_indexes(table)}
        if name in existing:
            op.drop_index(name, table_name=table)
