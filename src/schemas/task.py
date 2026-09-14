"""
task.py (schemas) — the richest schemas in the project. This file is where we
show off Pydantic's validation power, because tasks have the most rules.

Concepts demonstrated here (all common interview topics):
  - Field(...) constraints (min/max length, ge/le numeric bounds).
  - Enums as field types (TaskStatus).
  - @field_validator  — validate/normalize ONE field.
  - @model_validator  — validate ACROSS multiple fields.
  - Nested response models (TaskDetailOut embeds a list of CommentOut).
  - from_attributes so we can return ORM objects directly.
"""

from datetime import datetime, timezone

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# We reuse the SAME enum the ORM uses. One definition of "what statuses exist",
# shared by the DB layer and the API layer — no drift between them.
from src.models.task import TaskStatus
from src.schemas.comment import CommentOut


class TaskCreate(BaseModel):
    """Payload for POST /projects/{project_id}/tasks."""

    title: str = Field(min_length=1, max_length=200, examples=["Write the README"])
    description: str | None = Field(default=None, max_length=5000)
    # Default new tasks to "todo". Client may override with any valid status.
    status: TaskStatus = TaskStatus.TODO
    # ge=1, le=5 => "greater-or-equal 1 AND less-or-equal 5". Out-of-range values
    # are rejected with 422 automatically. No manual `if` needed.
    priority: int = Field(default=3, ge=1, le=5)
    assignee_id: str | None = None
    due_date: datetime | None = None

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, v: str) -> str:
        """
        A FIELD validator: runs for the `title` field AFTER type validation.

        Field(min_length=1) rejects an empty string, but NOT "   " (spaces),
        which is length 3 yet meaningless. So we strip whitespace and reject if
        nothing is left. We also RETURN the stripped value, so the stored title
        is normalized (no accidental leading/trailing spaces). Validators can
        both validate AND transform.

        @classmethod + (cls, v): the signature Pydantic v2 expects — it hands the
        class and the incoming value; you return the cleaned value (or raise).
        """
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("title must not be blank or whitespace-only")
        return cleaned

    @field_validator("due_date")
    @classmethod
    def due_date_must_be_future(cls, v: datetime | None) -> datetime | None:
        """
        Reject due dates in the past. Only runs if due_date was provided
        (v is not None). Shows conditional validation of an optional field.

        We compare in UTC to avoid naive-vs-aware datetime bugs. If the client
        sent a "naive" datetime (no timezone), we assume UTC.
        """
        if v is None:
            return v
        # Normalize to timezone-aware UTC for a correct comparison.
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        if v < datetime.now(timezone.utc):
            raise ValueError("due_date must be in the future")
        return v


class TaskUpdate(BaseModel):
    """
    Payload for PATCH /tasks/{id}. All fields optional => partial update.

    Subtle point: because every field defaults to None, we can't tell "user
    omitted this field" from "user explicitly sent null" by value alone. The
    service handles this with `model_dump(exclude_unset=True)`, which yields ONLY
    the fields the client actually included. That's the clean way to do PATCH.
    """

    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    status: TaskStatus | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    assignee_id: str | None = None
    due_date: datetime | None = None

    @model_validator(mode="after")
    def at_least_one_field(self) -> "TaskUpdate":
        """
        A MODEL validator (mode="after"): runs AFTER all fields are individually
        validated, and can inspect the WHOLE object (cross-field logic).

        Here we enforce that a PATCH isn't empty — sending {} should be a client
        error, not a silent no-op. We check whether ANY field was explicitly set
        by the client. model_fields_set is the set of field names the client
        actually provided.

        mode="after" gives us a fully-built model instance (self) with real
        typed values; mode="before" would run on the raw input dict instead.
        """
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided to update")
        return self


class TaskOut(BaseModel):
    """The standard task representation (used in lists and single fetches)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    description: str | None
    status: TaskStatus
    priority: int
    project_id: str
    assignee_id: str | None
    due_date: datetime | None
    created_at: datetime
    updated_at: datetime


class TaskDetailOut(TaskOut):
    """
    A RICHER task representation that also embeds the task's comments.

    WHY A SEPARATE, HEAVIER SCHEMA:
      List endpoints (GET /tasks) should stay lean — you don't want to load and
      serialize every comment for every task in a list (that's wasteful and
      invites the N+1 problem). So the list uses TaskOut, and only the single
      GET /tasks/{id} uses this TaskDetailOut, which includes comments. Tailoring
      the response to the endpoint is a real API-design skill.

    It INHERITS every field from TaskOut and just adds `comments`. `comments:
    list[CommentOut]` is a NESTED model — Pydantic serializes each ORM Comment
    into a CommentOut. We eager-load these in the service with selectinload() to
    avoid N+1 queries.
    """

    comments: list[CommentOut] = []
