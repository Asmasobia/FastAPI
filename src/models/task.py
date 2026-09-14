"""
task.py — the Task table (the heart of the domain) + the TaskStatus enum.

A Task belongs to a Project, may be assigned to a User, has a status and
priority, an optional due date, and many Comments.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from src.models.comment import Comment
    from src.models.project import Project
    from src.models.user import User


def _uuid_str() -> str:
    """Generate a random UUID string for use as a primary key (see user.py)."""
    return str(uuid.uuid4())


class TaskStatus(str, enum.Enum):
    """
    The allowed states of a task, as an ENUM.

    WHY `str, enum.Enum` (inheriting BOTH):
      - enum.Enum gives us a fixed, self-documenting set of choices, so an
        invalid status is impossible to store.
      - Also inheriting `str` means each member IS a string ("todo" == TaskStatus.TODO),
        which makes it serialize cleanly to JSON and store cleanly in the DB
        without extra conversion. This dual-inheritance is the idiomatic way to
        do string enums in Python.
    """

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class Task(Base, TimestampMixin):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_str
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False, index=True)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Store the enum. SQLEnum maps the Python enum to a DB column that only
    # accepts the defined values. default=TaskStatus.TODO sets new tasks to "todo".
    # index=True because we filter tasks by status constantly (GET /tasks?status=).
    status: Mapped[TaskStatus] = mapped_column(
        SQLEnum(TaskStatus, name="task_status"),
        default=TaskStatus.TODO,
        nullable=False,
        index=True,
    )

    # A simple integer priority (1=low .. 5=high). We validate the range in the
    # Pydantic schema (API boundary), keeping the DB column a plain int.
    priority: Mapped[int] = mapped_column(Integer, default=3, nullable=False)

    # Optional deadline. Nullable because not every task has a due date.
    due_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- FOREIGN KEYS ----
    # Which project this task lives in (required).
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Who it's assigned to (optional — a task can be unassigned).
    # ondelete="SET NULL": if the assignee's user row is deleted, don't delete
    # the task — just clear the assignment. Different cascade choice than above,
    # chosen because the task should survive losing its assignee.
    assignee_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ---- RELATIONSHIPS ----
    project: Mapped["Project"] = relationship(back_populates="tasks")

    assignee: Mapped["User | None"] = relationship(
        back_populates="assigned_tasks"
    )

    # A task's comments. Ordered by creation time so they read chronologically.
    # cascade delete-orphan: deleting a task deletes its comments.
    comments: Mapped[list["Comment"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="Comment.created_at",
    )
