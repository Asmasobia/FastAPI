"""
task_service.py — the richest service. This is where three interview-favorite
topics are implemented for real:

  1. AVOIDING N+1 QUERIES with selectinload (eager loading).
  2. CURSOR (keyset) PAGINATION.
  3. Dynamic FILTERING built up conditionally.

Read the comments in list_tasks and get_task_detail carefully — they're the
ones you'll be asked about.
"""

import base64
import binascii

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.exceptions import NotFoundError, PermissionDeniedError
from src.models.project import Project
from src.models.task import Task, TaskStatus
from src.models.user import User
from src.schemas.task import TaskCreate, TaskUpdate

# Import the ownership check so task operations verify the parent project too.
from src.services.project_service import get_owned_project


# ------------------------------------------------------------------------------
# CURSOR HELPERS
# ------------------------------------------------------------------------------
# A cursor is just "where to resume". We paginate by (created_at, id) so the
# order is stable even when two tasks share a created_at. We encode the last
# item's id as an OPAQUE base64 string. "Opaque" matters: clients must treat it
# as a black box and just echo it back, so we're free to change what's inside
# later without breaking them.
def _encode_cursor(task_id: str) -> str:
    return base64.urlsafe_b64encode(task_id.encode()).decode()


def _decode_cursor(cursor: str) -> str | None:
    """Decode a cursor back to a task id; return None if it's malformed."""
    try:
        return base64.urlsafe_b64decode(cursor.encode()).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


async def _get_task_owned_via_project(
    db: AsyncSession, task_id: str, user: User
) -> Task:
    """
    Load a task and verify the current user owns its parent project.

    Authorization for tasks is INHERITED from the project: you can touch a task
    iff you own the project it lives in. We join to the project and check its
    owner_id. One query does both the existence check and the ownership check.
    """
    result = await db.execute(
        select(Task)
        .join(Project, Task.project_id == Project.id)
        .where(Task.id == task_id)
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise NotFoundError(f"Task {task_id} not found")
    # We must load the project to check ownership. It's cheap and correct to
    # re-run the owned-project check, which raises 403 if not owned.
    await get_owned_project(db, task.project_id, user)
    return task


async def create_task(
    db: AsyncSession, project_id: str, data: TaskCreate, user: User
) -> Task:
    """Create a task inside a project the user owns."""
    # Ownership check first: can't add tasks to a project that isn't yours.
    await get_owned_project(db, project_id, user)

    task = Task(
        title=data.title,
        description=data.description,
        status=data.status,
        priority=data.priority,
        assignee_id=data.assignee_id,
        due_date=data.due_date,
        project_id=project_id,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


async def get_task_detail(
    db: AsyncSession, task_id: str, user: User
) -> Task:
    """
    Fetch ONE task WITH its comments eagerly loaded — the N+1 lesson.

    THE N+1 PROBLEM (guaranteed interview question):
      Say you load a task, then access task.comments. By default SQLAlchemy
      lazy-loads: it fires a SECOND query only when you touch .comments. For one
      task that's 2 queries. But list 50 tasks and touch each one's comments and
      you get 1 (list) + 50 (one per task) = 51 queries. That's the "N+1": one
      query to get the list, then N more, one per item. It murders performance.

      Worse in ASYNC code: a lazy load happens on attribute access, which isn't
      awaitable, so it raises (the "greenlet_spawn has not been called" error).
      In async SQLAlchemy you MUST eager-load anything you'll access.

    THE FIX — selectinload(Task.comments):
      Tells SQLAlchemy to eager-load comments up front using a SECOND query with
      `WHERE comment.task_id IN (...)`. So for any number of tasks it's exactly
      2 queries total (the tasks, then all their comments in one go), not N+1.
      (The alternative, joinedload, uses a JOIN — better for one-to-one;
      selectinload is usually better for one-to-many like comments because it
      avoids row duplication.)
    """
    result = await db.execute(
        select(Task)
        .where(Task.id == task_id)
        .options(selectinload(Task.comments))  # <-- eager-load, no N+1
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise NotFoundError(f"Task {task_id} not found")
    # Ownership via the parent project.
    await get_owned_project(db, task.project_id, user)
    return task


async def list_tasks(
    db: AsyncSession,
    user: User,
    *,
    project_id: str | None = None,
    status: TaskStatus | None = None,
    assignee_id: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> tuple[list[Task], str | None]:
    """
    List tasks for the current user's projects, with FILTERING + CURSOR
    PAGINATION. Returns (items, next_cursor).

    Returns a plain tuple rather than the Page schema — shaping the HTTP
    response is the router's job; the service just returns data + the cursor.
    """
    # Start from tasks that belong to projects OWNED by this user. We join to
    # Project and filter by owner so a user can never page through others' tasks.
    query = (
        select(Task)
        .join(Project, Task.project_id == Project.id)
        .where(Project.owner_id == user.id)
    )

    # ---- DYNAMIC FILTERS ----
    # Build the WHERE clause conditionally: only add a filter if the client
    # supplied that query param. This is the clean pattern for optional filters —
    # each `if` narrows the query further. SQLAlchemy lets us keep chaining
    # .where() and ANDs them together.
    if project_id is not None:
        query = query.where(Task.project_id == project_id)
    if status is not None:
        query = query.where(Task.status == status)
    if assignee_id is not None:
        query = query.where(Task.assignee_id == assignee_id)

    # ---- CURSOR PAGINATION ----
    # Stable ordering is REQUIRED for keyset pagination: we order by id (unique).
    # If a cursor was provided, resume strictly AFTER that id. Because ids are
    # random UUIDs we order lexicographically by id for a deterministic sequence
    # (the point here is to demonstrate the mechanism; a real app often paginates
    # by created_at + id). "Fetch rows with id > last_seen_id" is the keyset
    # predicate that replaces slow OFFSET.
    query = query.order_by(Task.id)
    if cursor is not None:
        last_id = _decode_cursor(cursor)
        if last_id is not None:
            query = query.where(Task.id > last_id)

    # Fetch limit + 1: the extra row tells us whether a NEXT page exists without
    # a separate COUNT query. If we get limit+1 rows back, there's more.
    query = query.limit(limit + 1)

    result = await db.execute(query)
    rows = list(result.scalars().all())

    # Determine next_cursor from that trick.
    next_cursor: str | None = None
    if len(rows) > limit:
        rows = rows[:limit]  # trim the peeked extra row.
        next_cursor = _encode_cursor(rows[-1].id)  # resume after the last item.

    return rows, next_cursor


async def update_task(
    db: AsyncSession, task_id: str, data: TaskUpdate, user: User
) -> Task:
    """Partial update of a task the user owns (via its project)."""
    task = await _get_task_owned_via_project(db, task_id, user)
    updates = data.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(task, field, value)
    await db.commit()
    await db.refresh(task)
    return task


async def delete_task(db: AsyncSession, task_id: str, user: User) -> None:
    """Delete a task the user owns. Cascade removes its comments."""
    task = await _get_task_owned_via_project(db, task_id, user)
    await db.delete(task)
    await db.commit()
