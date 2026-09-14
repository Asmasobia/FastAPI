"""
base.py — the DECLARATIVE BASE plus a reusable timestamp mixin.

WHAT "DECLARATIVE BASE" MEANS:
  In SQLAlchemy 2.x you define tables by subclassing a common `Base` class.
  Every model that inherits from Base is automatically registered on
  `Base.metadata` — a catalog of all tables. That catalog is what Alembic reads
  to generate migrations and what create_all() reads to build tables.

WHY A MIXIN FOR TIMESTAMPS:
  created_at / updated_at columns belong on almost every table. Rather than
  copy-pasting them into each model, we define them once in a mixin and inherit.
  DRY (Don't Repeat Yourself), and every table gets consistent behavior.
"""

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """
    The root of all ORM models.

    Subclassing DeclarativeBase (the 2.x style) gives us the modern typed
    mapping API: we declare columns as `Mapped[type]` annotations, which means
    the SAME annotation both describes the column to SQLAlchemy AND type-checks
    in your editor. Cleaner and safer than the old `Column(...)` style.
    """

    pass


class TimestampMixin:
    """
    Adds created_at and updated_at to any model that inherits it.

    `Mapped[datetime]` = "this attribute is a datetime column, and it's
    NOT NULL" (a bare Mapped[T] is non-nullable; Mapped[T | None] would allow
    NULL). mapped_column(...) supplies the column's details.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        # server_default=func.now() means the DATABASE fills this in at INSERT
        # time using its own clock (NOW()). Doing it server-side (vs. in Python)
        # keeps timestamps consistent even if multiple app servers have slightly
        # different clocks, and it works even for rows inserted outside the app.
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        # onupdate=func.now(): whenever a row is UPDATEd through the ORM,
        # refresh this column to the current time. Gives you a free "last
        # modified" timestamp.
        onupdate=func.now(),
        nullable=False,
    )
