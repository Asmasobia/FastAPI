"""
comments.py — endpoints for listing and adding comments on a task.

These paths are nested under a task (/tasks/{task_id}/comments) because a
comment only exists in the context of a task. Nesting the resource in the URL
communicates that relationship clearly to API consumers.
"""

from fastapi import APIRouter, status

from src.dependencies import CurrentUser, DbSession
from src.schemas.comment import CommentCreate, CommentOut
from src.services import comment_service

router = APIRouter(tags=["comments"])


@router.get(
    "/tasks/{task_id}/comments",
    response_model=list[CommentOut],
    summary="List a task's comments",
)
async def list_comments(
    task_id: str, user: CurrentUser, db: DbSession
) -> list[CommentOut]:
    """List all comments on a task (oldest first). Ownership enforced in service."""
    return await comment_service.list_comments(db, task_id, user)


@router.post(
    "/tasks/{task_id}/comments",
    response_model=CommentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a comment to a task",
)
async def create_comment(
    task_id: str, data: CommentCreate, user: CurrentUser, db: DbSession
) -> CommentOut:
    """
    Add a comment authored by the current user. The author is derived from the
    token in the service — the client can't spoof who wrote it.
    """
    return await comment_service.create_comment(db, task_id, data, user)
