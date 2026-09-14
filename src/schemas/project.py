"""
project.py (schemas) — request/response shapes for projects.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    """Payload for POST /projects."""

    name: str = Field(
        min_length=1,
        max_length=200,
        # examples=[...] shows up in the /docs "Try it out" panel as a
        # ready-to-use sample. Great DX and it's self-documenting.
        examples=["Website Redesign"],
    )
    description: str | None = Field(
        default=None,
        max_length=2000,
        examples=["Q3 marketing site overhaul"],
    )


class ProjectUpdate(BaseModel):
    """Payload for PATCH /projects/{id}. All optional -> partial update."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class ProjectOut(BaseModel):
    """What we return for a project."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str | None
    owner_id: str
    created_at: datetime
    updated_at: datetime
