"""Per-item reading state for the local reader."""

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, false
from sqlalchemy.orm import Mapped, mapped_column

from server.database.base import Base


class ReadingState(Base):
    __tablename__ = "reading_states"
    __table_args__ = (
        Index("ix_reading_states_saved", "saved"),
        Index("ix_reading_states_is_read", "is_read"),
    )

    content_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("content_items.id", ondelete="CASCADE"), primary_key=True
    )
    is_read: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    saved: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    # "like" (enjoyed) or "later" (read again). NULL means the item is saved
    # without a sub-label, which is how older backups import.
    tag: Mapped[str | None] = mapped_column(String(16))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
