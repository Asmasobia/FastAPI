"""
comment_service.py — create and list comments on a task.

Authorization, again, is inherited: you can comment on / read comments of a task
only if you own the project that task belongs to. We reuse the task ownership
helper so the rule lives in exactly one place.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.comment import Comment
from src.models.user import User
from src.schemas.comment import CommentCreate
from src.services.task_service import _get_task_owned_via_project


async def list_comments(
    db: AsyncSession, task_id: str, user: User
) -> list[Comment]:
    """List a task's comments (oldest first), after verifying ownership."""
    # Ownership check (raises 404/403 as appropriate).
    await _get_task_owned_via_project(db, task_id, user)

    result = await db.execute(
        select(Comment)
        .where(Comment.task_id == task_id)
        .order_by(Comment.created_at.asc())
    )
    return list(result.scalars().all())


async def create_comment(
    db: AsyncSession, task_id: str, data: CommentCreate, user: User
) -> Comment:
    """
    Add a comment to a task. The author is the CURRENT USER — taken from the
    token, never from the request body. (Letting the client set author_id would
    let anyone post as anyone.)
    """
    await _get_task_owned_via_project(db, task_id, user)

    comment = Comment(
        body=data.body,
        task_id=task_id,
        author_id=user.id,
    )
    db.add(comment)
    await db.commit()
    await db.refresh(comment)
    return comment
