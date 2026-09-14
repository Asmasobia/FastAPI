"""
project.py — the Project table.

A Project belongs to one owner (User) and groups many Tasks.
This is a classic ONE-TO-MANY (User 1—* Project, Project 1—* Task) demo.
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


class Project(Base, TimestampMixin):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=_uuid_str
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Text (vs String) = unbounded length column, right for freeform descriptions.
    # Mapped[str | None] makes the column NULLABLE — the description is optional.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- FOREIGN KEY: the actual link column ----
    # This stores the id of the owning user. ForeignKey("users.id") enforces at
    # the DB level that the value must match a real users.id.
    # ondelete="CASCADE": if the referenced user row is deleted directly in the
    # DB, the database removes dependent projects too. (Complements the ORM-side
    # cascade on User.projects; the DB-level rule is the real safety net.)
    # index=True: we frequently query "all projects for this owner", so index it.
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ---- RELATIONSHIPS ----
    # The other side of User.projects.
    owner: Mapped["User"] = relationship(back_populates="projects")

    # A project's tasks. Deleting a project deletes its tasks (delete-orphan).
    tasks: Mapped[list["Task"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
    )
