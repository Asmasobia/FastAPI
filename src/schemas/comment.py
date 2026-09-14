"""
comment.py (schemas) — request/response shapes for comments.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CommentCreate(BaseModel):
    """Payload for POST /tasks/{task_id}/comments."""

    body: str = Field(
        min_length=1,
        max_length=5000,
        examples=["I'll take this one, targeting Friday."],
    )


class CommentOut(BaseModel):
    """What we return for a comment."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    body: str
    task_id: str
    author_id: str
    created_at: datetime
