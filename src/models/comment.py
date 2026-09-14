"""
comment.py — the Comment table.

A Comment is written by a User (author) on a Task. This is the leaf of our
object graph and the thing we load with selectinload() to demonstrate avoiding
the N+1 query problem (see the tasks service).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from src.models.task import Task
    from src.models.user import User


def _uuid_str() -> str:
    return str(uuid.uuid4())


class Comment(Base, TimestampMixin):
    __tablename__ = "comments"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_str
    )

    body: Mapped[str] = mapped_column(Text, nullable=False)

    # Which task this comment is on (required).
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Who wrote it (required).
    author_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ---- RELATIONSHIPS ----
    task: Mapped["Task"] = relationship(back_populates="comments")
    author: Mapped["User"] = relationship(back_populates="comments")
