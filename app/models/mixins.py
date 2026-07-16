from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        default=datetime.now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False),
        default=datetime.now,
        onupdate=datetime.now,
    )
